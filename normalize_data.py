"""
normalize_data.py
-----------------
Normalizes customer-health signals from multiple CSV sources into one
account-level record for analysis.

The input directory is supplied by the caller. In a client run that is always
the selected client's input/ directory, resolved by ClientWorkspace, so no
ambient environment variable can redirect one client's run at another client's
data.

Required:
- <input_dir>/crm_accounts.csv

Optional:
- <input_dir>/product_usage.csv
- <input_dir>/support_tickets.csv
- <input_dir>/customer_engagement.csv
- <input_dir>/customer_sentiment.csv
- <input_dir>/billing.csv

Missing optional sources are represented explicitly in data_availability so
the analysis layer does not confuse missing data with a healthy zero value.

Client data is validated before processing. Required baseline fields must be
present and dates must be YYYY-MM-DD, so a bad export fails with a message
naming the source, account, and field rather than an unexplained exception.

All date arithmetic is measured from an injected effective date rather than
the wall clock, so a run can be reproduced exactly from its manifest.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from chia_errors import ClientRunError


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "sample_data"

DATE_FORMAT = "%Y-%m-%d"

NORMALIZED_FILENAME = "normalized_accounts.json"

REQUIRED_ACCOUNT_FIELDS = (
    "account_id",
    "account_name",
    "arr",
    "renewal_date",
)

OPTIONAL_SOURCES = (
    "product_usage.csv",
    "support_tickets.csv",
    "customer_engagement.csv",
    "customer_sentiment.csv",
    "billing.csv",
)


class DataValidationError(ClientRunError):
    """Raised when client data cannot be processed as supplied."""

    label = "Data validation failed"


def read_csv(filename, data_dir, required=False):
    path = Path(data_dir) / filename

    if not path.exists():
        if required:
            raise DataValidationError(
                f"Required data file not found: {path}\n"
                f"The customer account list is the one mandatory source.",
                "Place crm_accounts.csv in the client's input/ directory. "
                "sample_data/crm_accounts.csv shows the expected schema.",
            )
        return []

    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def describe_row(source, row_number, account_id=None):
    """A consistent way to point a client at the exact row that failed."""
    location = f"{source} (row {row_number})"

    if account_id:
        return f"{location}, account {account_id}"

    return location


def validate_crm_accounts(crm_accounts):
    """
    Validate the required baseline before any processing happens.

    Every problem across the whole file is collected and reported together so a
    client can fix one export in one pass instead of one row per run.
    """
    problems = []
    seen_ids = {}

    if not crm_accounts:
        raise DataValidationError(
            "crm_accounts.csv contains no account rows.",
            "Export at least one account. A header row alone is not enough "
            "to run a portfolio.",
        )

    for index, account in enumerate(crm_accounts, start=2):
        account_id = (account.get("account_id") or "").strip()
        where = describe_row("crm_accounts.csv", index, account_id or None)

        for field in REQUIRED_ACCOUNT_FIELDS:
            if field not in account:
                problems.append(
                    f"{where}: required column '{field}' is missing from the file."
                )
            elif (account.get(field) or "").strip() == "":
                problems.append(
                    f"{where}: required field '{field}' is blank."
                )

        if account_id:
            if account_id in seen_ids:
                problems.append(
                    f"{where}: duplicate account_id, already used on row "
                    f"{seen_ids[account_id]}."
                )
            else:
                seen_ids[account_id] = index

        arr = (account.get("arr") or "").strip()
        if arr:
            try:
                float(arr)
            except ValueError:
                problems.append(
                    f"{where}: field 'arr' must be a plain number without "
                    f"currency symbols or commas, got '{arr}'."
                )

        renewal_date = (account.get("renewal_date") or "").strip()
        if renewal_date:
            problems.extend(
                validate_date(
                    renewal_date,
                    "renewal_date",
                    "crm_accounts.csv",
                    index,
                    account_id,
                )
            )

        contract_start = (account.get("contract_start") or "").strip()
        if contract_start:
            problems.extend(
                validate_date(
                    contract_start,
                    "contract_start",
                    "crm_accounts.csv",
                    index,
                    account_id,
                )
            )

    if problems:
        raise DataValidationError(
            "Customer account data could not be processed:\n  - "
            + "\n  - ".join(problems),
            "Correct crm_accounts.csv in the client's input/ directory and "
            "run again. Every problem found in the file is listed above, so "
            "one pass should be enough.",
        )


def validate_date(value, field, source, row_number, account_id=None):
    """Return a list of problems (empty when the date is valid)."""
    try:
        datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        where = describe_row(source, row_number, account_id)
        return [
            f"{where}: field '{field}' must use the YYYY-MM-DD date format, "
            f"got '{value}'."
        ]

    return []


def validate_optional_dates(rows, source, fields):
    """
    Validate dates in the optional sources.

    These are not required, but a malformed value would otherwise surface as a
    raw strptime traceback with no indication of which file or row caused it.
    """
    problems = []

    for index, row in enumerate(rows, start=2):
        account_id = (row.get("account_id") or "").strip()

        for field in fields:
            value = (row.get(field) or "").strip()

            if value:
                problems.extend(
                    validate_date(
                        value, field, source, index, account_id or None
                    )
                )

    return problems


def int_or_none(value):
    if value in (None, ""):
        return None
    return int(float(value))


def float_or_none(value):
    if value in (None, ""):
        return None
    return float(value)


def days_until(date_string, as_of):
    """
    Days from the run's effective date to a future date.

    `as_of` is injected rather than read from the clock so that a run recorded
    in a manifest can be replayed exactly. Passing today's date reproduces the
    previous behaviour.
    """
    if not date_string:
        return None

    target_date = datetime.strptime(date_string, DATE_FORMAT)

    return (target_date - as_of).days


def days_since(date_string, as_of):
    if not date_string:
        return None

    event_date = datetime.strptime(date_string, DATE_FORMAT)

    return (as_of - event_date).days


def average(values):
    clean = [value for value in values if value is not None]

    if not clean:
        return None

    return round(sum(clean) / len(clean), 1)


def normalize_accounts(input_dir=None, output_dir=None, as_of=None):
    """
    Build one normalized account record per CRM account.

    `input_dir`  the directory holding this run's CSV exports. In a client run
                 this is always the selected client's input/ directory.
    `output_dir` where normalized_accounts.json is written. Pass None to skip
                 writing entirely (used by --validate-only).
    `as_of`      the run's effective date, used for all day arithmetic.

    Returns (accounts, source_report). The source report records which files
    were supplied and which rows referenced unknown accounts, so the run can
    report data-quality conditions without re-reading the inputs.
    """
    data_dir = Path(input_dir) if input_dir else DEFAULT_DATA_DIR
    as_of = as_of or datetime.today()

    crm_accounts = read_csv(
        "crm_accounts.csv",
        data_dir,
        required=True,
    )

    validate_crm_accounts(crm_accounts)

    product_usage = read_csv(
        "product_usage.csv",
        data_dir,
    )

    support_tickets = read_csv(
        "support_tickets.csv",
        data_dir,
    )

    customer_engagement = read_csv(
        "customer_engagement.csv",
        data_dir,
    )

    customer_sentiment = read_csv(
        "customer_sentiment.csv",
        data_dir,
    )

    billing = read_csv(
        "billing.csv",
        data_dir,
    )

    date_problems = []
    date_problems.extend(
        validate_optional_dates(
            product_usage, "product_usage.csv", ["last_active_date"]
        )
    )
    date_problems.extend(
        validate_optional_dates(
            support_tickets, "support_tickets.csv", ["created_date"]
        )
    )
    date_problems.extend(
        validate_optional_dates(
            customer_engagement,
            "customer_engagement.csv",
            ["last_csm_meeting", "last_exec_meeting"],
        )
    )
    date_problems.extend(
        validate_optional_dates(
            customer_sentiment, "customer_sentiment.csv", ["survey_date"]
        )
    )

    if date_problems:
        raise DataValidationError(
            "Customer health signal data could not be processed:\n  - "
            + "\n  - ".join(date_problems),
            "Dates must use the YYYY-MM-DD format. Correct the files listed "
            "above in the client's input/ directory and run again.",
        )

    source_presence = {
        "crm": bool(crm_accounts),
        "product_usage": bool(product_usage),
        "support": bool(support_tickets),
        "engagement": bool(customer_engagement),
        "sentiment": bool(customer_sentiment),
        "billing": bool(billing),
    }

    usage_by_account = {
        row["account_id"]: row
        for row in product_usage
    }

    engagement_by_account = {
        row["account_id"]: row
        for row in customer_engagement
    }

    sentiment_by_account = {
        row["account_id"]: row
        for row in customer_sentiment
    }

    billing_by_account = {
        row["account_id"]: row
        for row in billing
    }

    tickets_by_account = defaultdict(list)

    for ticket in support_tickets:
        tickets_by_account[
            ticket["account_id"]
        ].append(ticket)

    normalized_accounts = []

    for account in crm_accounts:
        account_id = account["account_id"]

        usage = usage_by_account.get(
            account_id
        )

        engagement = engagement_by_account.get(
            account_id
        )

        sentiment = sentiment_by_account.get(
            account_id
        )

        billing_record = billing_by_account.get(
            account_id
        )

        tickets = tickets_by_account.get(
            account_id,
            [],
        )

        account_source_presence = {
            "crm": True,
            "product_usage": usage is not None,
            "support": (
                source_presence["support"]
                and any(
                    ticket.get("account_id")
                    == account_id
                    for ticket in support_tickets
                )
            ),
            "engagement": engagement is not None,
            "sentiment": sentiment is not None,
            "billing": billing_record is not None,
        }

        usage = usage or {}
        engagement = engagement or {}
        sentiment = sentiment or {}
        billing_record = billing_record or {}

        high_severity_tickets = sum(
            1
            for ticket in tickets
            if ticket.get(
                "priority",
                "",
            ).lower()
            in {"high", "critical"}
        )

        open_tickets = sum(
            1
            for ticket in tickets
            if ticket.get(
                "status",
                "",
            ).lower()
            != "closed"
        )

        resolution_hours = [
            float_or_none(
                ticket.get(
                    "resolution_hours"
                )
            )
            for ticket in tickets
        ]

        csat_scores = [
            float_or_none(
                ticket.get("csat")
            )
            for ticket in tickets
        ]

        current_nps = int_or_none(
            sentiment.get("nps")
        )

        previous_nps = int_or_none(
            sentiment.get(
                "previous_nps"
            )
        )

        licensed_users = int_or_none(
            usage.get(
                "licensed_users"
            )
        )

        monthly_active_users = int_or_none(
            usage.get(
                "monthly_active_users"
            )
        )

        if (
            licensed_users
            and monthly_active_users
            is not None
        ):
            utilization = round(
                monthly_active_users
                / licensed_users
                * 100,
                1,
            )
        else:
            utilization = None

        if (
            current_nps is not None
            and previous_nps is not None
        ):
            nps_change = (
                current_nps
                - previous_nps
            )
        else:
            nps_change = None

        qbr_raw = engagement.get(
            "qbr_completed"
        )

        if qbr_raw in (None, ""):
            qbr_completed = None
        else:
            qbr_completed = (
                str(qbr_raw).lower()
                == "true"
            )

        normalized_record = {
            "account_id": account_id,
            "account_name": account[
                "account_name"
            ],
            "segment": account.get(
                "segment"
            ),
            "arr": float(
                account["arr"]
            ),
            "contract_start": account.get(
                "contract_start"
            ),
            "renewal_date": account.get(
                "renewal_date"
            ),
            "days_to_renewal": days_until(
                account.get(
                    "renewal_date"
                ),
                as_of,
            ),
            "csm": account.get(
                "csm"
            ),
            "industry": account.get(
                "industry"
            ),
            "employee_count": int_or_none(
                account.get(
                    "employee_count"
                )
            ),
            "account_stage": account.get(
                "account_stage"
            ),

            "data_availability":
                account_source_presence,

            "product_usage": {
                "monthly_active_users":
                    monthly_active_users,
                "licensed_users":
                    licensed_users,
                "license_utilization_pct":
                    utilization,
                "login_change_60d_pct":
                    float_or_none(
                        usage.get(
                            "login_change_60d_pct"
                        )
                    ),
                "core_feature_adoption_pct":
                    float_or_none(
                        usage.get(
                            "core_feature_adoption_pct"
                        )
                    ),
                "automation_adoption_pct":
                    float_or_none(
                        usage.get(
                            "automation_adoption_pct"
                        )
                    ),
                "usage_trend":
                    usage.get(
                        "usage_trend"
                    ),
                "last_active_date":
                    usage.get(
                        "last_active_date"
                    ),
            },

            "support": {
                "ticket_count": (
                    len(tickets)
                    if account_source_presence[
                        "support"
                    ]
                    else None
                ),
                "high_severity_tickets": (
                    high_severity_tickets
                    if account_source_presence[
                        "support"
                    ]
                    else None
                ),
                "open_tickets": (
                    open_tickets
                    if account_source_presence[
                        "support"
                    ]
                    else None
                ),
                "average_resolution_hours":
                    average(
                        resolution_hours
                    ),
                "average_csat":
                    average(
                        csat_scores
                    ),
            },

            "engagement": {
                "last_csm_meeting":
                    engagement.get(
                        "last_csm_meeting"
                    ),
                "days_since_last_csm_meeting":
                    days_since(
                        engagement.get(
                            "last_csm_meeting"
                        ),
                        as_of,
                    ),
                "last_exec_meeting":
                    engagement.get(
                        "last_exec_meeting"
                    ),
                "days_since_last_exec_meeting":
                    days_since(
                        engagement.get(
                            "last_exec_meeting"
                        ),
                        as_of,
                    ),
                "qbr_completed":
                    qbr_completed,
                "champion_status":
                    engagement.get(
                        "champion_status"
                    ),
                "stakeholder_coverage":
                    engagement.get(
                        "stakeholder_coverage"
                    ),
                "open_action_items":
                    int_or_none(
                        engagement.get(
                            "open_action_items"
                        )
                    ),
            },

            "sentiment": {
                "current_nps":
                    current_nps,
                "previous_nps":
                    previous_nps,
                "nps_change":
                    nps_change,
                "sentiment":
                    sentiment.get(
                        "sentiment"
                    ),
                "primary_feedback":
                    sentiment.get(
                        "primary_feedback"
                    ),
            },

            # Blank commercial cells stay None rather than collapsing to 0.
            # A blank is "not available", which the scoring model treats as an
            # unavailable signal. Only an explicit 0 means "none known".
            "billing": {
                "billing_status":
                    billing_record.get(
                        "billing_status"
                    ),
                "days_past_due":
                    int_or_none(
                        billing_record.get(
                            "days_past_due"
                        )
                    ),
                "contract_value":
                    float_or_none(
                        billing_record.get(
                            "contract_value"
                        )
                    ),
                "expansion_arr":
                    float_or_none(
                        billing_record.get(
                            "expansion_arr"
                        )
                    ),
                "contraction_arr":
                    float_or_none(
                        billing_record.get(
                            "contraction_arr"
                        )
                    ),
            },
        }

        normalized_accounts.append(
            normalized_record
        )

    source_report = build_source_report(
        data_dir,
        crm_accounts,
        {
            "product_usage.csv": product_usage,
            "support_tickets.csv": support_tickets,
            "customer_engagement.csv": customer_engagement,
            "customer_sentiment.csv": customer_sentiment,
            "billing.csv": billing,
        },
    )

    # output_dir is None under --validate-only. Validation must be able to run
    # without producing a deliverable.
    if output_dir is not None:
        write_normalized(normalized_accounts, output_dir)

    return normalized_accounts, source_report


def write_normalized(normalized_accounts, workspace):
    """
    Persist the normalized records through the client write gate.

    This deliberately accepts a ClientWorkspace and not a path. A raw directory
    argument here would be a second, ungated way to write to disk, which is
    exactly the thing client isolation has to rule out.
    """
    if not hasattr(workspace, "write_text"):
        raise DataValidationError(
            f"normalize_data can only write through a ClientWorkspace; got "
            f"{type(workspace).__name__}.",
            "This is an internal error. Report it with the run log.",
        )

    return workspace.write_text(
        NORMALIZED_FILENAME, json.dumps(normalized_accounts, indent=2)
    )


def build_source_report(data_dir, crm_accounts, optional_rows):
    """
    Record what was actually supplied, for the run manifest and warnings.

    Rows referencing an account_id that is not in crm_accounts.csv are silently
    ignored by the join above. That is the right behaviour, but it must be
    visible, so those ids are collected here.
    """
    known_ids = {
        (row.get("account_id") or "").strip()
        for row in crm_accounts
    }

    files_present = {"crm_accounts.csv": True}
    orphans = {}

    for filename in OPTIONAL_SOURCES:
        files_present[filename] = (Path(data_dir) / filename).exists()

        rows = optional_rows.get(filename) or []
        unmatched = {
            (row.get("account_id") or "").strip()
            for row in rows
            if (row.get("account_id") or "").strip() not in known_ids
        }

        orphans[filename] = sorted(identifier for identifier in unmatched)

    return {
        "input_dir": str(data_dir),
        "files_present": files_present,
        "row_counts": {
            "crm_accounts.csv": len(crm_accounts),
            **{
                filename: len(optional_rows.get(filename) or [])
                for filename in OPTIONAL_SOURCES
            },
        },
        "orphan_account_ids": orphans,
    }


if __name__ == "__main__":
    print(
        "normalize_data is a library module in a client run.\n"
        "Run the workflow with:\n"
        "    uv run python main.py clients/<client-name>"
    )
