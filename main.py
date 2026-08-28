"""
main.py
-------
The CHIA client run.

    uv run python main.py clients/<client-name>

One invocation serves exactly one client. The workspace is resolved and
validated before any I/O, the PLS baseline is loaded and merged with that
client's documented overrides, and every output is written through the
workspace write gate into that client's outputs directory and nowhere else.

The scoring engine in health_model.py is untouched by this layer. Everything
here is orchestration, presentation, and record-keeping.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

import client_config
import data_quality
import health_model
import run_metadata
from chia_errors import ClientRunError, ConfigError
from client_workspace import ClientWorkspace
from normalize_data import normalize_accounts


load_dotenv()

MODEL = "claude-haiku-4-5-20251001"
BASE_DIR = Path(__file__).resolve().parent

USAGE = """\
CHIA - Customer Health Intelligence Agent

Usage:
    uv run python main.py <client-workspace> [options]

Example:
    uv run python main.py clients/acme

Options:
    --validate-only     Validate the workspace, configuration, and input data.
                        Writes nothing. Use this before every client delivery.
    --no-ai             Skip narrative generation. Deterministic scoring and
                        reports only. No ANTHROPIC_API_KEY required.
    --no-slack          Suppress Slack alerts. Alerts that would have fired
                        are still counted and recorded in the run manifest.
    --as-of YYYY-MM-DD  Pin the run's effective date. Defaults to today.
                        Use the effective_date from a run manifest to
                        reproduce that run exactly.

A client workspace is a directory containing:

    config/client_config.yaml
    input/crm_accounts.csv          (plus any optional signal exports)
    outputs/                        (created automatically)

clients/_template is a documented starting point.
"""


def build_anthropic_client():
    """
    Create the narrative client.

    Deferred until a narrative is actually needed so that --validate-only and
    --no-ai runs do not require an API key.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set, so the AI narrative cannot be "
            "generated.",
            "Add ANTHROPIC_API_KEY to your .env file, or run with --no-ai to "
            "produce deterministic scoring and reports without narrative.",
        )

    return anthropic.Anthropic(api_key=api_key)


def clean_json_text(text):
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def format_date(date_string):
    if not date_string:
        return "N/A"

    year, month, day = date_string.split("-")
    return f"{month}/{day}/{year}"


def money(value):
    if value is None:
        return "Not available"

    return f"${value:,.0f}"


def or_na(value):
    if value is None:
        return "Not available"

    return value


def empty_narrative():
    return {
        "health_summary": "",
        "risk_drivers": [],
        "positive_signals": [],
        "expansion_evidence": ["No current expansion evidence"],
        "recommended_next_step": "",
    }


def _num(value):
    """Render a numeric signal value without a trailing .0."""
    if value is None:
        return "not available"

    if isinstance(value, float) and value.is_integer():
        return str(int(value))

    return str(value)


def _signed(value):
    """Render a change value so the direction is unambiguous."""
    if value is None:
        return "not available"

    return f"+{_num(value)}" if value > 0 else _num(value)


def _words(value):
    """Render a source-data enum as readable text without reinterpreting it."""
    return str(value).replace("_", " ")


# One fact-only sentence per scored signal, keyed by the (component,
# subcomponent) keys in config/health_rules.yaml rather than by display label,
# so relabelling a signal in configuration does not silently drop its evidence.
# Each sentence states source data only: no cause, commitment, SLA, intent, or
# commercial status is inferred.
SIGNAL_SENTENCES = {
    ("product_adoption", "license_utilization_pct"): lambda a: (
        f"License utilization is "
        f"{_num(a['product_usage']['license_utilization_pct'])}%."
    ),
    ("product_adoption", "login_change_60d_pct"): lambda a: (
        f"The 60-day login change is "
        f"{_signed(a['product_usage']['login_change_60d_pct'])}%."
    ),
    ("product_adoption", "core_feature_adoption_pct"): lambda a: (
        f"Core feature adoption is "
        f"{_num(a['product_usage']['core_feature_adoption_pct'])}%."
    ),
    ("product_adoption", "automation_adoption_pct"): lambda a: (
        f"Automation and advanced feature adoption is "
        f"{_num(a['product_usage']['automation_adoption_pct'])}%."
    ),
    ("customer_engagement", "days_since_last_csm_meeting"): lambda a: (
        f"The last CSM meeting was "
        f"{_num(a['engagement']['days_since_last_csm_meeting'])} days ago."
    ),
    ("customer_engagement", "days_since_last_exec_meeting"): lambda a: (
        f"The last executive meeting was "
        f"{_num(a['engagement']['days_since_last_exec_meeting'])} days ago."
    ),
    ("customer_engagement", "qbr_completed"): lambda a: (
        "A QBR has been completed."
        if a["engagement"]["qbr_completed"]
        else "No QBR has been completed."
    ),
    ("customer_engagement", "champion_status"): lambda a: (
        f"Champion status is recorded as "
        f"{_words(a['engagement']['champion_status'])}."
    ),
    ("customer_engagement", "stakeholder_coverage"): lambda a: (
        f"Stakeholder coverage is recorded as "
        f"{_words(a['engagement']['stakeholder_coverage'])}."
    ),
    ("customer_engagement", "open_action_items"): lambda a: (
        f"Open action items recorded: "
        f"{_num(a['engagement']['open_action_items'])}."
    ),
    ("customer_sentiment", "current_nps"): lambda a: (
        f"Current NPS is {_num(a['sentiment']['current_nps'])}."
    ),
    ("customer_sentiment", "nps_change"): lambda a: (
        f"NPS changed by {_signed(a['sentiment']['nps_change'])} points."
    ),
    ("customer_sentiment", "sentiment"): lambda a: (
        f"Recorded customer sentiment is {_words(a['sentiment']['sentiment'])}."
        + (
            f" Customer feedback: {a['sentiment']['primary_feedback']}"
            if a["sentiment"].get("primary_feedback") not in (None, "")
            else ""
        )
    ),
    ("support_health", "high_severity_tickets"): lambda a: (
        f"High-severity tickets recorded: "
        f"{_num(a['support']['high_severity_tickets'])}."
    ),
    ("support_health", "open_tickets"): lambda a: (
        f"Tickets currently open: {_num(a['support']['open_tickets'])}."
    ),
    ("support_health", "average_csat"): lambda a: (
        f"Average CSAT is {_num(a['support']['average_csat'])}."
    ),
    ("support_health", "average_resolution_hours"): lambda a: (
        f"Average resolution time is "
        f"{_num(a['support']['average_resolution_hours'])} hours."
    ),
    ("commercial_renewal", "known_contraction"): lambda a: (
        f"Known contraction of "
        f"{money(float(a['billing']['contraction_arr'] or 0))} is recorded in "
        f"the source data."
        if float(a["billing"]["contraction_arr"] or 0) > 0
        else "No known contraction is recorded in the source data."
    ),
    ("commercial_renewal", "billing_status"): lambda a: (
        "Billing is current with 0 days past due."
        if (a["billing"]["days_past_due"] or 0) <= 0
        else f"Billing is {_num(a['billing']['days_past_due'])} days past due."
    ),
    ("commercial_renewal", "renewal_proximity"): lambda a: (
        f"The account is {_num(a['days_to_renewal'])} days from renewal on "
        f"{format_date(a['renewal_date'])}."
    ),
}


def _signal_outcome_range(rule):
    """
    The worst and best point outcomes the configuration allows for one signal.

    Derived entirely from the configured scoring rules in health_rules.yaml. No
    new threshold is introduced here: "best" is the top configured outcome for
    that signal and "worst" is the bottom configured outcome.
    """
    outcomes = [band["points"] for band in (rule.get("bands") or [])]

    for key in (
        "below_points",
        "above_points",
        "true_points",
        "false_points",
        "none_points",
        "any_points",
        "current_points",
        "minor_overdue_points",
        "materially_overdue_points",
    ):
        if rule.get(key) is not None:
            outcomes.append(rule[key])

    outcomes.extend((rule.get("values") or {}).values())

    if not outcomes:
        return None, None

    return min(outcomes), max(outcomes)


def _signal_rules_by_label(component_rules):
    """Map a component's subcomponent labels back to their configured rules."""
    return {
        rule["label"]: (key, rule)
        for key, rule in component_rules.items()
        if isinstance(rule, dict) and "label" in rule
    }


def classify_signals(account, health, health_rules):
    """
    Sort the individual scored signals into adverse, positive, and neutral.

    Categorization is per signal, never per component. A component that loses a
    single point does not make its other signals adverse.

    Using the configured scoring rules and nothing else:

        earned at the signal's best configured outcome  -> Positive Signal
        earned at the signal's worst configured outcome -> Risk Driver
        anything in between                             -> neither

    Neutral signals are not forced into either list. They remain visible in
    Underlying Customer Signals. Unavailable signals are never categorized,
    because missing data is not an adverse fact.
    """
    model_rules = health_rules["health_model"]

    risk_drivers = []
    positive_signals = []

    for component in health["components"]:
        component_rules = model_rules.get(component["key"], {})
        rules_by_label = _signal_rules_by_label(component_rules)

        for item in component["subcomponents"]:
            if not item["available"]:
                continue

            entry = rules_by_label.get(item["label"])

            if entry is None:
                continue

            rule_key, rule = entry
            sentence_for = SIGNAL_SENTENCES.get((component["key"], rule_key))
            worst, best = _signal_outcome_range(rule)

            if sentence_for is None or worst is None:
                continue

            sentence = sentence_for(account)

            if item["earned"] >= best:
                positive_signals.append(sentence)
            elif item["earned"] <= worst:
                risk_drivers.append(sentence)

    return risk_drivers, positive_signals


def build_deterministic_narrative(account, health, health_rules):
    """
    Build all factual narrative sections from deterministic data only.

    Risk Drivers and Positive Signals are individual signals categorized by
    classify_signals against the configured scoring rules. The wording contains
    source facts only; it does not invent a cause, commitment, SLA, or
    commercial status. Known Contraction never appears as Expansion Evidence.
    """
    risk_drivers, positive_signals = classify_signals(
        account, health, health_rules
    )

    billing = account.get("billing", {})
    expansion = billing.get("expansion_arr")

    if expansion is not None and float(expansion or 0) > 0:
        expansion_evidence = [
            f"Known expansion of {money(float(expansion))} is recorded in the source data."
        ]
    else:
        expansion_evidence = ["No current expansion evidence"]

    risk_labels = [
        item["label"]
        for component in health["components"]
        for item in component["subcomponents"]
        if item["available"] and item["earned"] < item["possible"]
    ]

    summary = (
        f"{account['account_name']} is {health['health_status']} with "
        f"{health['retention_risk']} retention risk and a Health Score of "
        f"{health['health_score']}/100. "
    )

    days = account.get("days_to_renewal")
    if days is not None:
        summary += f"The account is {days} days from renewal. "

    if risk_labels:
        summary += "The scored signals below full available points are: " + ", ".join(risk_labels) + ". "
    else:
        summary += "All available scored signals earned their full available points. "

    contraction = billing.get("contraction_arr")
    if contraction is not None and float(contraction or 0) > 0:
        summary += (
            f"Known contraction of {money(float(contraction))} is recorded in the source data. "
        )

    if expansion is not None and float(expansion or 0) > 0:
        summary += (
            f"Known expansion of {money(float(expansion))} is recorded in the source data. "
        )

    if health["signal_coverage_pct"] < 100:
        unavailable = "; ".join(health.get("unavailable_signals", []))
        summary += (
            f"Signal Coverage is {health['signal_coverage_pct']}%."
            + (f" Unavailable signals: {unavailable}." if unavailable else "")
        )
    else:
        summary += "Signal Coverage is 100%."

    return {
        "health_summary": summary.strip(),
        "risk_drivers": risk_drivers,
        "positive_signals": positive_signals,
        "expansion_evidence": expansion_evidence,
        "recommended_next_step": "",
    }


def build_scoring_context(health):
    """
    A compact, readable view of the deterministic results for the LLM.

    The model is given the conclusions so it can explain them. It is not given
    the opportunity to recalculate them.
    """
    lines = [
        f"Health Score: {health['health_score']}/100 "
        f"({health['points_earned']} of "
        f"{health['points_available']} available points)",
        f"Health Status: {health['health_status']}",
        f"Retention Risk: {health['retention_risk']}",
        f"Signal Coverage: {health['signal_coverage_pct']}% "
        f"({health['points_available']} of "
        f"{health['points_configured']} configured points)",
        "",
        "Component scores (earned / available):",
    ]

    for component in health["components"]:
        lines.append(
            f"- {component['label']}: "
            f"{component['earned']}/{component['available']}"
        )

        for item in component["subcomponents"]:
            if item["available"]:
                lines.append(
                    f"    - {item['label']}: "
                    f"{item['earned']}/{item['possible']}"
                )
            else:
                lines.append(
                    f"    - {item['label']}: not available"
                )

    if health["unavailable_signals"]:
        lines.append("")
        lines.append(
            "Signals unavailable for this account: "
            + "; ".join(health["unavailable_signals"])
        )

    return "\n".join(lines)


def build_support_fact_context(account):
    """
    Present the support ticket counts as the separate facts they are.

    The normalized record carries a high-severity count and an open count. It
    does not carry the overlap between them, so the two are handed to the model
    as independent facts with the gap stated explicitly.
    """
    support = account.get("support", {})
    high_severity = support.get("high_severity_tickets")
    open_tickets = support.get("open_tickets")

    if high_severity is None and open_tickets is None:
        return []

    lines = ["", "INDEPENDENT SUPPORT TICKET COUNTS"]

    if high_severity is not None:
        lines.append(f"- High-severity tickets recorded: {high_severity}")

    if open_tickets is not None:
        lines.append(f"- Tickets currently open: {open_tickets}")

    lines.append(
        "- These are two separate counts of the same ticket population. The "
        "source data does not state how many of the open tickets are high "
        "severity, or how many of the high-severity tickets are still open. "
        "The overlap is unknown."
    )

    return lines


def analyze_account_narrative(account, health, deterministic_narrative, narrative_client):
    """
    Ask the LLM for one thing only: the recommended next step.

    All facts, evidence lists, scores, classifications, and commercial figures
    are already determined before this call.
    """
    scoring_context = build_scoring_context(health)
    evidence_context = "\n".join(
        [
            "DETERMINISTIC HEALTH SUMMARY",
            deterministic_narrative["health_summary"],
            "",
            "DETERMINISTIC RISK DRIVERS",
            *[f"- {item}" for item in deterministic_narrative["risk_drivers"]],
            "",
            "DETERMINISTIC POSITIVE SIGNALS",
            *[f"- {item}" for item in deterministic_narrative["positive_signals"]],
            "",
            "DETERMINISTIC EXPANSION EVIDENCE",
            *[f"- {item}" for item in deterministic_narrative["expansion_evidence"]],
            *build_support_fact_context(account),
        ]
    )

    prompt = f"""
You are a senior Customer Success Revenue Intelligence analyst.

Everything factual below has already been calculated and written
deterministically. Your only job is to recommend the next Customer Success
action. Do not rewrite the evidence and do not add new customer facts.

Return ONLY valid raw JSON using exactly this structure:

{{
  "recommended_next_step": "one concise, practical Customer Success action"
}}

HARD BOUNDARIES

- Use only the deterministic facts below.
- Do not invent or infer contractual commitments, SLAs, cancellation intent,
  renewal intent, root causes, or customer decisions.
- High-severity ticket count and open-ticket count are independent facts. Their
  overlap is unknown unless the source data explicitly establishes it.
- Never describe open tickets as high-severity merely because both counts exist.
- Never describe high-severity tickets as open merely because both counts exist.
- Do not add, subtract, or otherwise combine the two counts.
- If the action refers to both, refer to them separately. For example:
  "Review the 2 currently open tickets and separately assess the 3 recorded
  high-severity tickets." Never write "the 2 open high-severity tickets".
- Known Contraction and Known Expansion are source-data amounts. Do not describe
  them as agreed, contracted, committed, signed, or in a renewal contract unless
  that wording appears in the deterministic evidence.
- A recommended timeline may be proposed, but clearly frame it as a recommended
  action, not an existing customer requirement.
- Prefer review, validate, confirm, investigate, align, triage, and discuss when
  the source facts do not establish a stronger conclusion.
- Human review and customer engagement remain the final decision points.

DETERMINISTIC SCORING RESULTS

{scoring_context}

{evidence_context}
"""

    response = narrative_client.messages.create(
        model=MODEL,
        max_tokens=350,
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )

    raw_text = response.content[0].text
    cleaned = clean_json_text(raw_text)

    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        print(
            f"Recommendation parse failed for "
            f"{account['account_name']}: {error}"
        )
        print("Raw response:", raw_text)
        return None

    recommendation = payload.get("recommended_next_step")
    if not isinstance(recommendation, str) or not recommendation.strip():
        return None

    return recommendation.strip()


def build_account_result(account, health, narrative):
    """
    Assemble the final account result.

    Every classification, dollar figure, summary, and evidence list here is
    deterministic. Only Recommended Next Step may be AI-assisted.
    """
    billing = account["billing"]
    billing_available = account["data_availability"]["billing"]

    contraction = billing.get("contraction_arr")
    expansion = billing.get("expansion_arr")

    result = dict(narrative)

    result["health_score"] = health["health_score"]
    result["health_status"] = health["health_status"]
    result["retention_risk"] = health["retention_risk"]
    result["signal_coverage_pct"] = health["signal_coverage_pct"]
    result["points_earned"] = health["points_earned"]
    result["points_available"] = health["points_available"]
    result["points_configured"] = health["points_configured"]
    result["components"] = health["components"]
    result["unavailable_signals"] = health["unavailable_signals"]

    # Revenue Exposure is not predicted loss. It is current ARR associated
    # with accounts assessed as having meaningful retention risk.
    result["revenue_exposure"] = (
        float(account["arr"])
        if health["retention_risk"] in {"Medium", "High"}
        else 0.0
    )

    # Known commercial movement comes only from client/source data. A blank or
    # absent billing record means nothing is known, not that the value is zero.
    result["commercial_data_available"] = billing_available
    result["known_contraction"] = float(contraction or 0)
    result["known_expansion"] = float(expansion or 0)
    result["known_contraction_available"] = (
        billing_available and contraction is not None
    )
    result["known_expansion_available"] = (
        billing_available and expansion is not None
    )

    # Expansion Opportunity is independent of attention routing and never
    # affects the Health Score.
    result["expansion_opportunity"] = result["known_expansion"] > 0

    return result


def get_priority_group(account, result, health_rules):
    """
    Primary attention routing.

    Three mutually exclusive groups. Expansion is deliberately not one of them.

    The rules arrive as an argument rather than a module global so a run always
    routes against its own client's resolved configuration. The routing logic
    itself is unchanged and is not client-configurable.
    """
    priority_rules = health_rules["priority_routing"]

    if (
        result["retention_risk"]
        in set(priority_rules["executive_retention_risk"])
        or result["health_status"]
        in set(priority_rules["executive_health_status"])
        or (
            priority_rules["executive_on_known_contraction"]
            and result["known_contraction"] > 0
        )
    ):
        return "Executive Attention"

    if (
        result["retention_risk"]
        in set(priority_rules["csm_retention_risk"])
        or result["health_status"]
        in set(priority_rules["csm_health_status"])
    ):
        return "CSM Attention"

    return "Healthy / No Immediate Action"


def should_alert(account, result, health_rules):
    alert_rules = health_rules["alerts"]

    days_to_renewal = account.get("days_to_renewal")
    within_renewal_window = (
        days_to_renewal is not None
        and days_to_renewal
        <= alert_rules["high_risk_max_days_to_renewal"]
    )

    high_revenue_risk = (
        result["retention_risk"] == "High"
        and result["revenue_exposure"]
        >= alert_rules["high_risk_min_arr"]
        and within_renewal_window
    )

    known_contraction_risk = (
        result["known_contraction"]
        >= alert_rules["known_contraction_min_arr"]
    )

    material_expansion = (
        result["known_expansion"]
        >= alert_rules["known_expansion_min_arr"]
        and (
            not alert_rules["expansion_alert_requires_low_risk"]
            or result["retention_risk"] == "Low"
        )
    )

    return (
        high_revenue_risk
        or known_contraction_risk
        or material_expansion
    )


def render_score_breakdown(result):
    rows = [
        "| Component | Points Earned | Points Available |",
        "|---|---:|---:|",
    ]

    for component in result["components"]:
        rows.append(
            f"| {component['label']} | {component['earned']} | "
            f"{component['available']} |"
        )

    rows.append(
        f"| **Total** | **{result['points_earned']}** | "
        f"**{result['points_available']}** |"
    )

    return "\n".join(rows)


def render_evidence(items, empty_note):
    """Render an evidence list, or a stated absence when there is nothing."""
    if not items:
        return f"- {empty_note}"

    return "\n".join(f"- {item}" for item in items)


def save_markdown_report(account, result, workspace):
    # The filename is derived from a CRM-supplied account name, so it is
    # slugified and written through the workspace gate. A name containing a
    # path separator cannot steer this write out of the client's outputs.
    filename = workspace.report_filename(account["account_name"])

    usage = account["product_usage"]
    support = account["support"]
    engagement = account["engagement"]
    sentiment = account["sentiment"]
    billing = account["billing"]

    unavailable = result["unavailable_signals"]

    coverage_note = (
        "All configured signals were available for this account."
        if not unavailable
        else "Signals not available for this account: "
        + "; ".join(unavailable)
        + "."
    )

    content = f"""# Customer Health Intelligence Audit — {account['account_name']}

## Executive Account Summary

| Metric | Current Position |
|---|---|
| Current ARR | ${account['arr']:,.0f} |
| Renewal Date | {format_date(account['renewal_date'])} |
| Days to Renewal | {account['days_to_renewal']} |
| Health Score | {result['health_score']}/100 |
| Health Status | {result['health_status']} |
| Retention Risk | {result['retention_risk']} |
| Signal Coverage | {result['signal_coverage_pct']}% |
| Revenue Exposure | ${result['revenue_exposure']:,.0f} |
| Known Contraction | {money(result['known_contraction']) if result['known_contraction_available'] else 'Not available'} |
| Known Expansion | {money(result['known_expansion']) if result['known_expansion_available'] else 'Not available'} |
| Expansion Opportunity | {'Yes' if result['expansion_opportunity'] else 'No'} |

### Health Summary

{result['health_summary']}

## Health Score Breakdown

{render_score_breakdown(result)}

Health Score is the points earned across available signals, normalized to 100. Signal Coverage is the share of the configured model that this account's data supported ({result['points_available']} of {result['points_configured']} configured points).

{coverage_note}

## Why This Account Is Here

### Risk Drivers

{render_evidence(result['risk_drivers'], "No scored signal is at its lowest configured outcome for this account.")}

### Positive Signals

{render_evidence(result['positive_signals'], "No scored signal is at its highest configured outcome for this account.")}

### Expansion Evidence

{render_evidence(result['expansion_evidence'], "No current expansion evidence")}

---

## Underlying Customer Signals

### Product Usage

- **Monthly Active Users:** {or_na(usage['monthly_active_users'])} of {or_na(usage['licensed_users'])} licensed users
- **License Utilization:** {or_na(usage['license_utilization_pct'])}%
- **60-Day Login Change:** {or_na(usage['login_change_60d_pct'])}%
- **Core Feature Adoption:** {or_na(usage['core_feature_adoption_pct'])}%
- **Automation Adoption:** {or_na(usage['automation_adoption_pct'])}%
- **Usage Trend:** {or_na(usage['usage_trend'])}

### Support

- **Total Tickets:** {or_na(support['ticket_count'])}
- **High-Severity Tickets:** {or_na(support['high_severity_tickets'])}
- **Open Tickets:** {or_na(support['open_tickets'])}
- **Average Resolution Time:** {or_na(support['average_resolution_hours'])} hours
- **Average CSAT:** {or_na(support['average_csat'])}

### Customer Engagement

- **Days Since CSM Meeting:** {or_na(engagement['days_since_last_csm_meeting'])}
- **Days Since Executive Meeting:** {or_na(engagement['days_since_last_exec_meeting'])}
- **QBR Completed:** {or_na(engagement['qbr_completed'])}
- **Champion Status:** {or_na(engagement['champion_status'])}
- **Stakeholder Coverage:** {or_na(engagement['stakeholder_coverage'])}
- **Open Action Items:** {or_na(engagement['open_action_items'])}

### Customer Sentiment

- **Current NPS:** {or_na(sentiment['current_nps'])}
- **Previous NPS:** {or_na(sentiment['previous_nps'])}
- **NPS Change:** {or_na(sentiment['nps_change'])} points
- **Current Sentiment:** {or_na(sentiment['sentiment'])}
- **Customer Feedback:** {or_na(sentiment['primary_feedback'])}

### Commercial Position

- **Current ARR:** ${account['arr']:,.0f}
- **Billing Status:** {or_na(billing['billing_status'])}
- **Days Past Due:** {or_na(billing['days_past_due'])}
- **Known Contraction:** {money(billing['contraction_arr'])}
- **Known Expansion:** {money(billing['expansion_arr'])}

---

## Recommended Next Step

{result['recommended_next_step']}

## Revenue Intelligence Definitions

- **Health Score:** Deterministic PLS Baseline Health Model result. Points earned across available signals, normalized to 100. Not generated by AI and not a financial forecast.
- **Health Status:** Determined from the Health Score using configured score bands.
- **Retention Risk:** Determined from Health Status. Low, Medium, or High.
- **Signal Coverage:** The share of the configured scoring model supported by this account's available data. It is a measure of data completeness, not statistical confidence.
- **Revenue Exposure:** Current ARR associated with an account assessed as having meaningful retention risk. This is not a prediction that this revenue will be lost.
- **Known Contraction:** Revenue reduction explicitly identified in the source data.
- **Known Expansion:** Additional recurring revenue explicitly identified in the source data.
- **Risk Drivers / Positive Signals:** Individual scored signals sitting at their lowest / highest configured outcome under the PLS baseline. A signal between those two points is neither, and stays visible under Underlying Customer Signals. A component losing a single point does not make its other signals adverse.
- **Expansion Opportunity:** Flagged from Known Expansion in the source data. Assessed independently of attention routing and it never improves the Health Score.
- **Commercial Movement:** Known Contraction and Known Expansion are sourced directly from the client data, not generated by AI.
- **AI contribution:** Recommended Next Step is AI-assisted. Health Summary, Risk Drivers, Positive Signals, Expansion Evidence, scores, classifications, and commercial figures are deterministic.
"""

    path = workspace.write_text(filename, content)

    print(f"Saved report: {path}")


def send_slack_alert(account, result, webhook_url):
    if not webhook_url:
        print(
            "Slack webhook missing. "
            "Skipping alert."
        )
        return False

    if result["risk_drivers"]:
        attention_details = chr(10).join(
            f"• {driver}"
            for driver in result["risk_drivers"]
        )
        attention_heading = "Why This Account Requires Attention"

    elif result["expansion_evidence"]:
        attention_details = chr(10).join(
            f"• {item}"
            for item in result["expansion_evidence"]
        )
        attention_heading = "Why This Expansion Opportunity Matters"

    else:
        attention_details = "• No additional account signals identified."
        attention_heading = "Account Intelligence"

    revenue_lines = []

    if result["revenue_exposure"] > 0:
        revenue_lines.append(
            f"*💰 Revenue Exposure:* "
            f"${result['revenue_exposure']:,.0f}"
        )

    if result["known_contraction"] > 0:
        revenue_lines.append(
            f"*📉 Known Contraction:* "
            f"${result['known_contraction']:,.0f}"
        )

    if result["known_expansion"] > 0:
        revenue_lines.append(
            f"*📈 Known Expansion:* "
            f"${result['known_expansion']:,.0f}"
        )

    revenue_summary = "\n".join(revenue_lines)

    message = f"""
🚨 *Customer Health Intelligence Alert*

*Account:* {account['account_name']}
*Current ARR:* ${account['arr']:,.0f}
*Renewal:* {format_date(account['renewal_date'])}
*Days to Renewal:* {account['days_to_renewal']}

━━━━━━━━━━

*❤️ Health:* {result['health_score']}/100 — {result['health_status']}
*⚠️ Retention Risk:* {result['retention_risk']}
*📶 Signal Coverage:* {result['signal_coverage_pct']}%
{revenue_summary}

*{attention_heading}*
{attention_details}

*Recommended Next Step*
{result['recommended_next_step']}

━━━━━━━━━━
"""

    try:
        response = requests.post(
            webhook_url,
            json={"text": message},
            timeout=10,
        )
    except requests.RequestException as error:
        # Alerting is a notification channel, not a result. A Slack outage
        # must not lose a portfolio that has already been scored.
        print(
            f"Slack request failed for "
            f"{account['account_name']}: {error}"
        )
        return False

    if response.status_code == 200:
        print(
            f"Slack alert sent for "
            f"{account['account_name']}."
        )
        return True

    print(
        "Slack error:",
        response.status_code,
        response.text,
    )

    return False


def summarize_reason(result):
    if result["risk_drivers"]:
        return result["risk_drivers"][0]

    # An account with no adverse signal is better described by what is actually
    # positive than by the expansion placeholder.
    if result["positive_signals"]:
        return result["positive_signals"][0]

    if result["expansion_evidence"]:
        return result["expansion_evidence"][0]

    return result["health_summary"]


def generate_portfolio_summary(
    portfolio_results,
    workspace,
    health_rules,
    client_name,
    effective_date,
):
    total_accounts = len(portfolio_results)

    total_arr = sum(
        item["account"]["arr"]
        for item in portfolio_results
    )

    health_counts = {
        "Healthy": 0,
        "Monitor": 0,
        "At Risk": 0,
        "Critical": 0,
    }

    for item in portfolio_results:
        status = item["result"]["health_status"]

        if status in health_counts:
            health_counts[status] += 1

    high_risk_arr = sum(
        item["account"]["arr"]
        for item in portfolio_results
        if item["result"]["retention_risk"] == "High"
    )

    medium_risk_arr = sum(
        item["account"]["arr"]
        for item in portfolio_results
        if item["result"]["retention_risk"] == "Medium"
    )

    total_revenue_exposure = sum(
        item["result"]["revenue_exposure"]
        for item in portfolio_results
    )

    total_contraction = sum(
        item["result"]["known_contraction"]
        for item in portfolio_results
    )

    total_expansion = sum(
        item["result"]["known_expansion"]
        for item in portfolio_results
    )

    net_known_movement = (
        total_expansion - total_contraction
    )

    priority_groups = {
        "Executive Attention": [],
        "CSM Attention": [],
        "Healthy / No Immediate Action": [],
    }

    for item in portfolio_results:
        group = get_priority_group(
            item["account"],
            item["result"],
            health_rules,
        )

        item["priority_group"] = group
        priority_groups[group].append(item)

    # Expansion is evaluated independently, so an account can appear here and
    # in any attention group at the same time.
    expansion_opportunities = [
        item
        for item in portfolio_results
        if item["result"]["expansion_opportunity"]
    ]

    generated_date = datetime.now().strftime(
        "%m/%d/%Y %I:%M %p"
    )

    # The client is named on the deliverable so a portfolio report can never be
    # filed against the wrong account when it leaves this repository.
    content = f"""# Customer Health Intelligence Audit — Portfolio Summary

**Client:** {client_name}
**Generated:** {generated_date}
**Effective Date:** {effective_date}

## Portfolio Snapshot

| Metric | Current Position |
|---|---:|
| Accounts Analyzed | {total_accounts} |
| Total Portfolio ARR | ${total_arr:,.0f} |
| Healthy | {health_counts['Healthy']} |
| Monitor | {health_counts['Monitor']} |
| At Risk | {health_counts['At Risk']} |
| Critical | {health_counts['Critical']} |

## Revenue Intelligence

| Metric | Current Position |
|---|---:|
| High-Risk ARR | ${high_risk_arr:,.0f} |
| Medium-Risk ARR | ${medium_risk_arr:,.0f} |
| Revenue Exposure | ${total_revenue_exposure:,.0f} |
| Known Contraction | ${total_contraction:,.0f} |
| Known Expansion | ${total_expansion:,.0f} |
| Net Known Movement | ${net_known_movement:,.0f} |

> Revenue Exposure represents current ARR associated with accounts showing meaningful retention risk. It is not predicted revenue loss and should be reviewed alongside the underlying account signals.

## Attention Summary

| Group | Accounts |
|---|---:|
| Executive Attention | {len(priority_groups['Executive Attention'])} |
| CSM Attention | {len(priority_groups['CSM Attention'])} |
| Healthy / No Immediate Action | {len(priority_groups['Healthy / No Immediate Action'])} |
| Expansion Opportunities | {len(expansion_opportunities)} |

> Expansion Opportunities are counted independently of attention routing. An account can require Executive or CSM Attention and still represent a genuine expansion opportunity.

---

## Portfolio Priorities
"""

    for group_name in [
        "Executive Attention",
        "CSM Attention",
        "Healthy / No Immediate Action",
    ]:
        content += f"\n### {group_name}\n\n"

        items = priority_groups[group_name]

        if not items:
            content += "No accounts currently in this category.\n"
            continue

        for item in sorted(
            items,
            key=lambda x: x["account"]["arr"],
            reverse=True,
        ):
            account = item["account"]
            result = item["result"]

            content += (
                f"#### {account['account_name']}\n\n"
                f"- **ARR:** ${account['arr']:,.0f}\n"
                f"- **Renewal:** {format_date(account['renewal_date'])} "
                f"({account['days_to_renewal']} days)\n"
                f"- **Health:** {result['health_score']}/100 — "
                f"{result['health_status']}\n"
                f"- **Retention Risk:** {result['retention_risk']}\n"
                f"- **Signal Coverage:** {result['signal_coverage_pct']}%\n"
            )

            if result["revenue_exposure"] > 0:
                content += (
                    f"- **Revenue Exposure:** "
                    f"${result['revenue_exposure']:,.0f}\n"
                )

            if result["known_contraction"] > 0:
                content += (
                    f"- **Known Contraction:** "
                    f"${result['known_contraction']:,.0f}\n"
                )

            if result["known_expansion"] > 0:
                content += (
                    f"- **Known Expansion:** "
                    f"${result['known_expansion']:,.0f}\n"
                )

            if result["expansion_opportunity"]:
                content += (
                    "- **Expansion Opportunity:** Yes\n"
                )

            content += (
                f"- **Primary Signal:** "
                f"{summarize_reason(result)}\n"
                f"- **Recommended Action:** "
                f"{result['recommended_next_step']}\n\n"
            )

    content += "\n## Expansion Opportunities\n\n"

    if not expansion_opportunities:
        content += (
            "No accounts currently show known expansion in the source data.\n"
        )
    else:
        content += (
            "Identified from Known Expansion in the source data, independently "
            "of attention routing.\n\n"
        )

        for item in sorted(
            expansion_opportunities,
            key=lambda x: x["result"]["known_expansion"],
            reverse=True,
        ):
            account = item["account"]
            result = item["result"]

            content += (
                f"#### {account['account_name']}\n\n"
                f"- **Known Expansion:** "
                f"${result['known_expansion']:,.0f}\n"
                f"- **Attention Group:** {item['priority_group']}\n"
                f"- **Health:** {result['health_score']}/100 — "
                f"{result['health_status']}\n"
                f"- **Retention Risk:** {result['retention_risk']}\n"
                f"- **Expansion Evidence:** "
                f"{result['expansion_evidence'][0] if result['expansion_evidence'] else 'Not provided'}\n\n"
            )

    content += """---

## How to Read This Report

- **Executive Attention:** Accounts with high retention risk, critical health, or known contraction requiring leadership visibility.
- **CSM Attention:** Accounts showing meaningful warning signals that require active management but may not yet require executive escalation.
- **Healthy / No Immediate Action:** Accounts currently showing healthy retention conditions.
- **Expansion Opportunities:** Accounts with explicit known expansion in the source data. Evaluated independently of attention routing, so an account can appear both here and in an attention group.
- **Health Score:** Produced by the deterministic PLS Baseline Health Model, not by AI.
- **Signal Coverage:** How much of the configured scoring model each account's data supported.
- **Known Contraction / Expansion:** Commercial amounts explicitly present in source data. These are not AI-generated estimates.
"""

    output_path = workspace.write_text(
        "portfolio-health-summary.md",
        content,
    )

    return {
        "path": output_path,
        "total_accounts": total_accounts,
        "total_arr": total_arr,
        "health_counts": health_counts,
        "high_risk_arr": high_risk_arr,
        "medium_risk_arr": medium_risk_arr,
        "revenue_exposure": total_revenue_exposure,
        "known_contraction": total_contraction,
        "known_expansion": total_expansion,
        "net_known_movement": net_known_movement,
        "priority_groups": priority_groups,
        "expansion_opportunities": expansion_opportunities,
    }


def print_portfolio_summary(summary, slack_alert_count, narrative_failures):
    """Internal run log. Not the customer-facing portfolio report."""
    print("\n")
    print("=" * 64)
    print(
        "CUSTOMER HEALTH INTELLIGENCE — "
        "PORTFOLIO COMPLETE"
    )
    print("=" * 64)

    print(
        f"Accounts Analyzed:        "
        f"{summary['total_accounts']}"
    )

    print(
        f"Total Portfolio ARR:      "
        f"${summary['total_arr']:,.0f}"
    )

    print("")

    print(
        f"Healthy:                  "
        f"{summary['health_counts']['Healthy']}"
    )

    print(
        f"Monitor:                  "
        f"{summary['health_counts']['Monitor']}"
    )

    print(
        f"At Risk:                  "
        f"{summary['health_counts']['At Risk']}"
    )

    print(
        f"Critical:                 "
        f"{summary['health_counts']['Critical']}"
    )

    print("")

    print(
        f"High-Risk ARR:            "
        f"${summary['high_risk_arr']:,.0f}"
    )

    print(
        f"Medium-Risk ARR:          "
        f"${summary['medium_risk_arr']:,.0f}"
    )

    print(
        f"Revenue Exposure:         "
        f"${summary['revenue_exposure']:,.0f}"
    )

    print(
        f"Known Contraction:        "
        f"${summary['known_contraction']:,.0f}"
    )

    print(
        f"Known Expansion:          "
        f"${summary['known_expansion']:,.0f}"
    )

    print(
        f"Net Known Movement:       "
        f"${summary['net_known_movement']:,.0f}"
    )

    print("")

    print(
        f"Executive Attention:      "
        f"{len(summary['priority_groups']['Executive Attention'])}"
    )

    print(
        f"CSM Attention:            "
        f"{len(summary['priority_groups']['CSM Attention'])}"
    )

    print(
        f"Healthy / No Action:      "
        f"{len(summary['priority_groups']['Healthy / No Immediate Action'])}"
    )

    print(
        f"Expansion Opportunities:  "
        f"{len(summary['expansion_opportunities'])}"
    )

    print("")

    # Internal operational counters only. Not customer-facing metrics.
    print(
        f"[internal] Slack Alerts:  "
        f"{slack_alert_count}"
    )

    print(
        f"[internal] Narrative Gaps:"
        f" {narrative_failures}"
    )

    print("")

    print(
        "Portfolio Report:         "
        f"{summary['path']}"
    )

    print("=" * 64)


# ---------------------------------------------------------------------------
# Client run
# ---------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="main.py",
        add_help=False,
        usage=argparse.SUPPRESS,
    )
    parser.add_argument("client", nargs="?")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--no-slack", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument("-h", "--help", action="store_true", dest="help")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2) from None

    if args.help:
        print(USAGE)
        raise SystemExit(0)

    # D4: a run without an explicit client workspace exits with usage. There is
    # deliberately no default and no fallback to sample_data, because an
    # implicit target is how one client's data ends up in another's report.
    if not args.client:
        print(USAGE, file=sys.stderr)
        print(
            "Error: no client workspace given. CHIA will not guess which "
            "client to run.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    return args


def resolve_effective_date(raw):
    """
    The date all day-arithmetic is measured from.

    Returned at midnight so a run is reproducible: `days_to_renewal` is a whole
    number of calendar days from this date, not from the moment the run
    happened to start.
    """
    if not raw:
        today = datetime.today()
        return (
            datetime(today.year, today.month, today.day),
            "defaulted to today",
        )

    try:
        parsed = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError:
        raise ConfigError(
            f"--as-of must be a date in YYYY-MM-DD format; got {raw!r}.",
            "For example: --as-of 2026-08-27",
        ) from None

    return parsed, "pinned with --as-of"


def resolve_slack_webhook(client, no_slack):
    """
    Per-client Slack routing.

    D6: the client configuration names an environment variable; the webhook
    itself only ever lives in .env. Returns (webhook_url, reason_if_disabled).
    """
    if no_slack:
        return None, "suppressed with --no-slack"

    slack = client["slack"]

    if not slack["enabled"]:
        return None, "not enabled in client configuration"

    webhook = os.getenv(slack["webhook_env_var"])

    if not webhook:
        return None, (
            f"environment variable {slack['webhook_env_var']} is not set"
        )

    return webhook, None


def print_run_header(workspace, client, effective_date, date_source, flags):
    print("=" * 64)
    print("CUSTOMER HEALTH INTELLIGENCE — CLIENT RUN")
    print("=" * 64)
    print(f"Client:          {client['name']}")
    print(f"Workspace:       {workspace.display_root()}")
    print(f"Input:           {workspace.input_dir}")
    print(f"Outputs:         {workspace.outputs_dir}")
    print(
        f"Effective Date:  {effective_date.strftime('%m/%d/%Y')} "
        f"({date_source})"
    )

    if client["overrides"]:
        print(f"Overrides:       {len(client['overrides'])} applied")
        for entry in client["overrides"]:
            print(f"                 - {entry['path']}")
    else:
        print("Overrides:       none (PLS baseline)")

    active = [name for name, on in flags.items() if on]
    print(f"Mode:            {', '.join(active) if active else 'full run'}")
    print("=" * 64)


def run(args):
    started_at = datetime.now()

    # 1-3. Identify the workspace and confirm it can be read. Nothing is
    # written and no configuration is loaded until this succeeds.
    workspace = ClientWorkspace.resolve(args.client)

    if os.getenv("CUSTOMER_HEALTH_DATA_DIR"):
        print(
            "Note: CUSTOMER_HEALTH_DATA_DIR is set in the environment and is "
            "ignored in a client run.\n"
            f"      Input is read from {workspace.input_dir}\n"
        )

    # 4-5. PLS baseline, then documented client overrides. Any override naming
    # an unknown or locked path stops the run here.
    baseline_path = client_config.CANONICAL_BASELINE
    baseline_rules = client_config.load_baseline_rules(baseline_path)
    client = client_config.load_client_config(workspace, baseline_rules)
    health_rules = client_config.resolve_health_rules(
        baseline_rules, client["overrides"]
    )

    effective_date, date_source = resolve_effective_date(args.as_of)

    print_run_header(
        workspace,
        client,
        effective_date,
        date_source,
        {
            "validate-only": args.validate_only,
            "no-ai": args.no_ai,
            "no-slack": args.no_slack,
        },
    )

    # 6. Normalize. Validation of required fields and dates happens inside and
    # raises before any output is produced.
    print("\nConsolidating customer data from source systems...")

    accounts, source_report = normalize_accounts(
        input_dir=workspace.input_dir,
        output_dir=None if args.validate_only else workspace,
        as_of=effective_date,
    )

    print(
        f"Created {len(accounts)} normalized account health records."
    )

    narrative_client = None

    if not args.validate_only and not args.no_ai:
        narrative_client = build_anthropic_client()

    webhook, slack_disabled_reason = resolve_slack_webhook(
        client, args.no_slack or args.validate_only
    )

    portfolio_results = []
    slack_alert_count = 0
    slack_suppressed_count = 0
    narrative_failures = 0

    # 7-9. Deterministic scoring first, then narrative, then outputs.
    for account in accounts:
        print(f"\nAnalyzing: {account['account_name']}")

        health = health_model.score_account(account, health_rules)

        # All factual narrative is deterministic and therefore repeatable.
        narrative = build_deterministic_narrative(account, health, health_rules)

        recommendation_available = False

        if narrative_client is not None:
            recommendation = analyze_account_narrative(
                account,
                health,
                narrative,
                narrative_client,
            )

            if recommendation is None:
                narrative_failures += 1
                print("Proceeding without AI-assisted recommendation.")
            else:
                narrative["recommended_next_step"] = recommendation
                recommendation_available = True
        else:
            narrative["recommended_next_step"] = (
                "AI-assisted recommendation was not generated for this run."
            )

        result = build_account_result(account, health, narrative)
        result["narrative_available"] = recommendation_available

        print(
            "Health:",
            f"{result['health_score']}/100 - {result['health_status']}",
        )
        print("Retention Risk:", result["retention_risk"])
        print("Signal Coverage:", f"{result['signal_coverage_pct']}%")
        print("Revenue Exposure:", f"${result['revenue_exposure']:,.0f}")
        print("Known Contraction:", f"${result['known_contraction']:,.0f}")
        print("Known Expansion:", f"${result['known_expansion']:,.0f}")

        if not args.validate_only:
            save_markdown_report(account, result, workspace)

        portfolio_results.append({"account": account, "result": result})

        if should_alert(account, result, health_rules):
            if webhook:
                if send_slack_alert(account, result, webhook):
                    slack_alert_count += 1
                else:
                    slack_suppressed_count += 1
            else:
                slack_suppressed_count += 1

    warnings = data_quality.collect(
        accounts, portfolio_results, health_rules, source_report
    )
    warning_summary = data_quality.summarize(warnings)

    if args.validate_only:
        return report_validation_only(
            workspace,
            client,
            accounts,
            portfolio_results,
            warnings,
            warning_summary,
            effective_date,
            date_source,
        )

    # 10. Portfolio outputs, all through the workspace write gate.
    summary = generate_portfolio_summary(
        portfolio_results,
        workspace,
        health_rules,
        client["name"],
        effective_date.strftime("%m/%d/%Y"),
    )

    resolved_config_text = client_config.dump_resolved_rules(
        health_rules, client, client["overrides"], baseline_path
    )
    workspace.write_text(
        run_metadata.RESOLVED_CONFIG_FILENAME, resolved_config_text
    )

    print_portfolio_summary(summary, slack_alert_count, narrative_failures)

    if slack_disabled_reason and slack_suppressed_count:
        print(
            f"[internal] Slack alerts not sent: {slack_suppressed_count} "
            f"({slack_disabled_reason})"
        )

    print("\nData Quality")
    print(data_quality.render_console(warnings))

    # 11. Run metadata, written last so it can record every other output.
    manifest = run_metadata.build(
        workspace=workspace,
        client=client,
        baseline_path=baseline_path,
        resolved_config_text=resolved_config_text,
        scored=portfolio_results,
        portfolio=summary,
        warnings=warnings,
        warning_summary=warning_summary,
        started_at=started_at,
        effective_date=effective_date,
        flags={
            "command": " ".join(["main.py"] + sys.argv[1:]),
            "validate_only": args.validate_only,
            "no_ai": args.no_ai,
            "no_slack": args.no_slack,
            "as_of": args.as_of,
            "as_of_source": date_source,
        },
        model=MODEL if narrative_client is not None else None,
        narrative_failures=narrative_failures,
        alerts_sent=slack_alert_count,
        alerts_suppressed=slack_suppressed_count,
        validation_status="passed",
    )

    manifest_path = run_metadata.write(workspace, manifest)

    print(f"\nRun manifest:             {manifest_path}")
    print(f"Outputs written:          {len(workspace.written_files)}")
    print(f"All outputs confined to:  {workspace.outputs_dir}")

    return 0


def report_validation_only(
    workspace,
    client,
    accounts,
    portfolio_results,
    warnings,
    warning_summary,
    effective_date,
    date_source,
):
    """
    --validate-only: prove the run would succeed, write nothing.

    Scoring is executed because a configuration that cannot score an account is
    a real defect, and it is better found here than during a delivery.
    """
    coverages = [
        item["result"]["signal_coverage_pct"] for item in portfolio_results
    ]

    print("\n")
    print("=" * 64)
    print("VALIDATION PASSED - NO FILES WRITTEN")
    print("=" * 64)
    print(f"Client:                   {client['name']}")
    print(f"Accounts validated:       {len(accounts)}")
    print(
        f"Portfolio ARR:            "
        f"${sum(a['arr'] for a in accounts):,.0f}"
    )
    print(
        f"Average Signal Coverage:  "
        f"{round(sum(coverages) / len(coverages), 1)}%"
    )
    print(
        f"Signal Coverage range:    "
        f"{min(coverages)}% to {max(coverages)}%"
    )
    print(f"Effective date:           {effective_date:%m/%d/%Y} ({date_source})")
    print(
        f"Overrides applied:        "
        f"{len(client['overrides'])}"
    )
    print(
        f"Data-quality conditions:  {warning_summary['total']} "
        f"({warning_summary['warnings']} warning, "
        f"{warning_summary['info']} info)"
    )
    print("")
    print(data_quality.render_console(warnings))
    print("=" * 64)
    print(
        "Nothing was written. Re-run without --validate-only to produce the "
        "client deliverable."
    )

    return 0


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        return run(args)
    except ClientRunError as error:
        print("\n" + "=" * 64, file=sys.stderr)
        print(error.render(), file=sys.stderr)
        print("=" * 64, file=sys.stderr)
        print("\nNo further outputs were produced.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

