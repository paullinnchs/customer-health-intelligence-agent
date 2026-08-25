import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
import requests
import yaml
from dotenv import load_dotenv

import health_model
from normalize_data import DataValidationError, normalize_accounts


load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

MODEL = "claude-haiku-4-5-20251001"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
CONFIG_PATH = BASE_DIR / "config" / "health_rules.yaml"


def load_health_rules():
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


HEALTH_RULES = load_health_rules()
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


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


def analyze_account_narrative(account, health):
    """
    Ask the LLM for interpretation only.

    Scores, statuses, risk levels, and dollar figures are all determined before
    this call and passed in as context.
    """
    account_json = json.dumps(account, indent=2)
    scoring_context = build_scoring_context(health)

    prompt = f"""
You are a senior Customer Success Revenue Intelligence analyst.

The customer's Health Score, Health Status, Retention Risk, and Signal
Coverage have ALREADY been calculated deterministically by the PLS Baseline
Health Model. Those results are given to you below and are final.

Your job is interpretation and narrative only. Explain what the evidence
means and what should happen next.

Return ONLY valid raw JSON.
No markdown.
No explanation outside the JSON.

Use this exact structure:

{{
  "health_summary": "concise explanation of the customer's current condition",
  "risk_drivers": [
    "specific evidence-based risk driver"
  ],
  "positive_signals": [
    "specific evidence-based positive signal"
  ],
  "expansion_evidence": [
    "specific evidence supporting expansion, or No current expansion evidence"
  ],
  "recommended_next_step": "specific Customer Success action based on the evidence"
}}

HARD BOUNDARIES

- Do NOT output a health score, health status, retention risk, churn risk, or
  any numeric rating of your own. Those are already decided.
- Do NOT calculate, estimate, or predict revenue loss.
- Do NOT invent contraction or expansion dollar amounts. Commercial figures
  come only from the source data.
- Do NOT contradict, re-rank, or argue with the deterministic classifications.
  If the evidence looks mixed, explain the tension instead.
- Do NOT infer facts that are not in the source data or the scoring results.
- CSAT is reported on the client's own survey scale, which is not stated in the
  data. Quote the value on its own ("average CSAT of 2.4") and never state or
  imply a denominator. Do not write "2.4/10", "2.4/5", or "2.4 out of 10".
- High-severity ticket count and open ticket count are separate, independent
  figures. A high-severity ticket is not necessarily open and an open ticket is
  not necessarily high-severity. Never merge them into one claim such as
  "three high-severity tickets remain open" unless both counts support it.

HEALTH SUMMARY

- Explain the customer's current condition in a few sentences.
- Reference the deterministic results where it helps the reader understand why
  the account sits where it does.
- If Signal Coverage is below 100%, note which signals were unavailable when
  that materially limits the picture.

RISK DRIVERS

- Identify the most important negative signals behind the assessment.
- Every risk driver must be traceable to the account data or to a low-scoring
  component in the results above.
- Be specific and quantify wherever possible.
- Example: "Product logins declined 31% over 60 days" is better than
  "Usage is declining."

POSITIVE SIGNALS

- Identify evidence supporting retention, adoption, customer value, or
  stability.
- Every positive signal must be traceable to the provided data or results.
- Include positive signals even for unhealthy accounts when they exist.

EXPANSION EVIDENCE

- Report expansion signals only when supported by the source data.
- Known Expansion in the billing data is a confirmed commercial fact; treat it
  as such and do not restate it as a dollar estimate of your own.
- Additional supporting evidence may include increasing usage, strong
  adoption, positive sentiment, or stakeholder engagement.
- Expansion is assessed independently of retention risk. An at-risk account
  can still carry a genuine expansion opportunity; say so if the evidence
  supports it.
- If there is no meaningful expansion evidence, say
  "No current expansion evidence."

RECOMMENDED NEXT STEP

- Recommend one clear Customer Success action.
- Use professional Customer Success language.
- Avoid terms such as "emergency" unless the source data explicitly indicates
  a confirmed cancellation or non-renewal decision.
- For high-risk accounts, prefer language such as executive intervention,
  recovery plan, renewal risk review, or success plan.
- Human review and customer engagement remain the final decision points.

DETERMINISTIC SCORING RESULTS

{scoring_context}

CUSTOMER ACCOUNT DATA

{account_json}
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
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
        narrative = json.loads(cleaned)

    except (json.JSONDecodeError, TypeError, ValueError) as error:
        print(
            f"Narrative parse failed for "
            f"{account['account_name']}: {error}"
        )
        print("Raw response:", raw_text)
        return None

    result = empty_narrative()

    for field in result:
        if field in narrative:
            result[field] = narrative[field]

    return result


def build_account_result(account, health, narrative):
    """
    Assemble the final account result.

    Every classification and every dollar figure here is deterministic. The
    narrative fields are the only LLM contribution.
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


def get_priority_group(account, result):
    """
    Primary attention routing.

    Three mutually exclusive groups. Expansion is deliberately not one of them.
    """
    priority_rules = HEALTH_RULES["priority_routing"]

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


def should_alert(account, result):
    alert_rules = HEALTH_RULES["alerts"]

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


def save_markdown_report(account, result):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = (
        account["account_name"]
        .lower()
        .replace(" ", "-")
        + "-health-intelligence.md"
    )

    path = OUTPUT_DIR / filename

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

{chr(10).join(f"- {driver}" for driver in result['risk_drivers'])}

### Positive Signals

{chr(10).join(f"- {signal}" for signal in result['positive_signals'])}

### Expansion Evidence

{chr(10).join(f"- {item}" for item in result['expansion_evidence'])}

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
- **Expansion Opportunity:** Flagged from Known Expansion in the source data. Assessed independently of attention routing and it never improves the Health Score.
- **Commercial Movement:** Known Contraction and Known Expansion are sourced directly from the client data, not generated by AI.
- **AI contribution:** Health Summary, Risk Drivers, Positive Signals, Expansion Evidence, and Recommended Next Step are AI-assisted interpretation of the evidence above.
"""

    path.write_text(
        content,
        encoding="utf-8",
    )

    print(f"Saved report: {path}")


def send_slack_alert(account, result):
    if not SLACK_WEBHOOK_URL:
        print(
            "Slack webhook missing. "
            "Skipping alert."
        )
        return

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

    response = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": message},
        timeout=10,
    )

    if response.status_code == 200:
        print(
            f"Slack alert sent for "
            f"{account['account_name']}."
        )
    else:
        print(
            "Slack error:",
            response.status_code,
            response.text,
        )


def summarize_reason(result):
    if result["risk_drivers"]:
        return result["risk_drivers"][0]

    if result["expansion_evidence"]:
        return result["expansion_evidence"][0]

    if result["positive_signals"]:
        return result["positive_signals"][0]

    return result["health_summary"]


def generate_portfolio_summary(portfolio_results):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

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

    content = f"""# Customer Health Intelligence Audit — Portfolio Summary

**Generated:** {generated_date}

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

    output_path = (
        OUTPUT_DIR
        / "portfolio-health-summary.md"
    )

    output_path.write_text(
        content,
        encoding="utf-8",
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


def main():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file before running."
        )

    print(
        "ANTHROPIC KEY LOADED:",
        bool(ANTHROPIC_API_KEY),
    )

    print(
        "SLACK WEBHOOK LOADED:",
        bool(SLACK_WEBHOOK_URL),
    )

    print(
        "\nConsolidating customer data "
        "from source systems..."
    )

    try:
        accounts = normalize_accounts()
    except DataValidationError as error:
        print("\nData validation failed.\n")
        print(error)
        raise SystemExit(1)

    print(
        f"Created {len(accounts)} "
        "normalized account health records."
    )

    portfolio_results = []
    slack_alert_count = 0
    narrative_failures = 0

    for account in accounts:
        print(
            f"\nAnalyzing: "
            f"{account['account_name']}"
        )

        health = health_model.score_account(
            account,
            HEALTH_RULES,
        )

        narrative = analyze_account_narrative(
            account,
            health,
        )

        # The score is deterministic, so a narrative failure must not drop the
        # account out of the portfolio and skew the totals.
        if narrative is None:
            narrative_failures += 1
            narrative = empty_narrative()
            narrative["health_summary"] = (
                "Narrative generation was unavailable for this account. "
                "The deterministic scoring results below are unaffected."
            )
            print(
                "Proceeding with deterministic results only."
            )

        result = build_account_result(
            account,
            health,
            narrative,
        )

        print(
            "Health:",
            f"{result['health_score']}/100 - "
            f"{result['health_status']}",
        )

        print(
            "Retention Risk:",
            result["retention_risk"],
        )

        print(
            "Signal Coverage:",
            f"{result['signal_coverage_pct']}%",
        )

        print(
            "Revenue Exposure:",
            f"${result['revenue_exposure']:,.0f}",
        )

        print(
            "Known Contraction:",
            f"${result['known_contraction']:,.0f}",
        )

        print(
            "Known Expansion:",
            f"${result['known_expansion']:,.0f}",
        )

        save_markdown_report(
            account,
            result,
        )

        portfolio_results.append(
            {
                "account": account,
                "result": result,
            }
        )

        if should_alert(
            account,
            result,
        ):
            send_slack_alert(
                account,
                result,
            )

            slack_alert_count += 1

    if portfolio_results:
        summary = generate_portfolio_summary(
            portfolio_results
        )

        print_portfolio_summary(
            summary,
            slack_alert_count,
            narrative_failures,
        )


if __name__ == "__main__":
    main()
