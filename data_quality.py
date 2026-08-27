"""
data_quality.py
---------------
Data-quality conditions observed during a client run.

These are conditions a client should know about but which do not stop the run.
A stop condition means the result cannot be trusted; a warning here means the
result is sound but incomplete, and the report will say so.

The most important category is unrecognized vocabulary. When a client's export
says champion_status: "champion" and the model's vocabulary has no such value,
the subcomponent is reported as UNAVAILABLE and Signal Coverage drops. That is
correct and deliberate — the alternative is guessing what the client meant —
but it must never happen quietly, so every unmapped value is surfaced by name.
"""

WARNING = "warning"
INFO = "info"


# Fields scored from a controlled vocabulary, and where their map lives in the
# resolved configuration.
VOCABULARY_FIELDS = (
    (
        "champion_status",
        "engagement",
        ("customer_engagement", "champion_status"),
        "customer_engagement.csv",
    ),
    (
        "stakeholder_coverage",
        "engagement",
        ("customer_engagement", "stakeholder_coverage"),
        "customer_engagement.csv",
    ),
    (
        "sentiment",
        "sentiment",
        ("customer_sentiment", "sentiment"),
        "customer_sentiment.csv",
    ),
)

OPTIONAL_SOURCE_LABELS = {
    "product_usage": "product_usage.csv",
    "support": "support_tickets.csv",
    "engagement": "customer_engagement.csv",
    "sentiment": "customer_sentiment.csv",
    "billing": "billing.csv",
}


def _warning(severity, code, message, accounts=None, detail=None):
    entry = {
        "severity": severity,
        "code": code,
        "message": message,
    }

    if accounts:
        entry["accounts"] = sorted(accounts)

    if detail:
        entry["detail"] = detail

    return entry


def _unknown_vocabulary(accounts, rules):
    """
    Report client values the model's vocabulary does not recognize.

    Reported per field with the offending values and the accounts affected, so
    the client can either correct the export or request a calibration.
    """
    findings = []
    model = rules["health_model"]

    for field, section, (component, subcomponent), source in (
        VOCABULARY_FIELDS
    ):
        known = {
            str(key).strip().lower()
            for key in model[component][subcomponent]["values"]
        }

        unmapped = {}

        for account in accounts:
            raw = account[section].get(field)

            if raw is None or str(raw).strip() == "":
                continue

            key = str(raw).strip().lower()

            if key not in known:
                unmapped.setdefault(str(raw).strip(), []).append(
                    account["account_name"]
                )

        for value, affected in sorted(unmapped.items()):
            label = model[component][subcomponent]["label"]

            findings.append(
                _warning(
                    WARNING,
                    "unrecognized_vocabulary",
                    f"{label}: the value \"{value}\" in {source} is not in "
                    f"the configured vocabulary. This subcomponent was "
                    f"reported as unavailable for "
                    f"{len(affected)} account(s) and Signal Coverage was "
                    f"reduced accordingly. The value was NOT guessed at or "
                    f"mapped to a nearby term.",
                    accounts=affected,
                    detail={
                        "field": field,
                        "source": source,
                        "value": value,
                        "recognized_values": sorted(known),
                    },
                )
            )

    return findings


def _missing_sources(accounts, source_report):
    findings = []

    for key, filename in OPTIONAL_SOURCE_LABELS.items():
        if source_report["files_present"].get(filename):
            continue

        findings.append(
            _warning(
                WARNING,
                "missing_optional_source",
                f"{filename} was not supplied. Every signal it feeds is "
                f"reported as unavailable for all accounts, which lowers "
                f"Signal Coverage across the portfolio.",
                detail={"source": filename},
            )
        )

    for key, filename in OPTIONAL_SOURCE_LABELS.items():
        if not source_report["files_present"].get(filename):
            continue

        missing = [
            account["account_name"]
            for account in accounts
            if not account["data_availability"][key]
        ]

        if missing:
            findings.append(
                _warning(
                    WARNING,
                    "missing_account_rows",
                    f"{filename} was supplied but has no rows for "
                    f"{len(missing)} account(s). Those signals are "
                    f"unavailable for the accounts listed.",
                    accounts=missing,
                    detail={"source": filename},
                )
            )

    return findings


def _orphan_rows(source_report):
    findings = []

    for filename, ids in sorted(source_report["orphan_account_ids"].items()):
        if not ids:
            continue

        findings.append(
            _warning(
                WARNING,
                "orphan_rows",
                f"{filename} contains {len(ids)} row(s) for account_id "
                f"values that do not appear in crm_accounts.csv. Those rows "
                f"were ignored.",
                detail={
                    "source": filename,
                    "account_ids": sorted(ids),
                },
            )
        )

    return findings


def _renewal_conditions(accounts):
    past_due = [
        account["account_name"]
        for account in accounts
        if account["days_to_renewal"] is not None
        and account["days_to_renewal"] < 0
    ]

    if not past_due:
        return []

    return [
        _warning(
            WARNING,
            "renewal_date_in_past",
            f"{len(past_due)} account(s) have a renewal_date earlier than "
            f"the run's effective date. They were scored in the final "
            f"Renewal Proximity band. Confirm whether these accounts have "
            f"renewed and the export is stale.",
            accounts=past_due,
        )
    ]


def _coverage_conditions(scored):
    """
    Report Signal Coverage gaps.

    Coverage is a locked, deterministic metric; this only makes the resulting
    gaps legible. No threshold here influences any score.
    """
    incomplete = [
        (item["account"]["account_name"], item["result"]["signal_coverage_pct"])
        for item in scored
        if item["result"]["signal_coverage_pct"] < 100
    ]

    if not incomplete:
        return []

    incomplete.sort(key=lambda pair: pair[1])

    return [
        _warning(
            INFO,
            "partial_signal_coverage",
            f"{len(incomplete)} account(s) were scored on less than the full "
            f"configured model. Health Scores for these accounts are "
            f"normalized against the signals that were available.",
            detail={
                "accounts": [
                    {"account_name": name, "signal_coverage_pct": pct}
                    for name, pct in incomplete
                ]
            },
        )
    ]


def collect(accounts, scored, rules, source_report):
    """
    Assemble every data-quality condition observed in this run.

    `accounts` are normalized records, `scored` the per-account results, and
    `source_report` the load-time observations from normalize_data.
    """
    findings = []
    findings.extend(_missing_sources(accounts, source_report))
    findings.extend(_orphan_rows(source_report))
    findings.extend(_unknown_vocabulary(accounts, rules))
    findings.extend(_renewal_conditions(accounts))
    findings.extend(_coverage_conditions(scored))

    return findings


def summarize(findings):
    return {
        "total": len(findings),
        "warnings": sum(1 for f in findings if f["severity"] == WARNING),
        "info": sum(1 for f in findings if f["severity"] == INFO),
    }


def render_console(findings):
    if not findings:
        return "No data-quality conditions detected."

    lines = []

    for finding in findings:
        marker = "!" if finding["severity"] == WARNING else "-"
        lines.append(f"  [{marker}] {finding['message']}")

        if finding.get("accounts"):
            shown = finding["accounts"][:5]
            suffix = (
                f" (+{len(finding['accounts']) - 5} more)"
                if len(finding["accounts"]) > 5
                else ""
            )
            lines.append(f"      Accounts: {', '.join(shown)}{suffix}")

    return "\n".join(lines)
