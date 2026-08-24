import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
import requests
import yaml
from dotenv import load_dotenv

from normalize_data import normalize_accounts


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


def derive_health_status(score):
    bands = HEALTH_RULES["health_score_bands"]

    if score >= bands["healthy_min"]:
        return "Healthy"

    if score >= bands["monitor_min"]:
        return "Monitor"

    if score >= bands["at_risk_min"]:
        return "At Risk"

    return "Critical"


def normalize_analysis_result(account, result):
    """
    Keep interpretive work with the LLM, but derive financial facts and
    routing-sensitive fields deterministically from the source data.
    """
    score = int(round(float(result.get("health_score", 0))))
    score = max(0, min(100, score))

    churn_risk = str(result.get("churn_risk", "Medium")).title()
    if churn_risk not in {"Low", "Medium", "High"}:
        churn_risk = "Medium"

    billing = account["billing"]

    result["health_score"] = score
    result["health_status"] = derive_health_status(score)
    result["churn_risk"] = churn_risk

    # Revenue Exposure is not predicted loss. It is current ARR associated
    # with accounts assessed as having meaningful retention risk.
    result["revenue_exposure"] = (
        float(account["arr"])
        if churn_risk in {"Medium", "High"}
        else 0.0
    )

    # Known commercial movement comes only from client/source data.
    result["known_contraction"] = float(
        billing.get("contraction_arr", 0) or 0
    )
    result["known_expansion"] = float(
        billing.get("expansion_arr", 0) or 0
    )

    result.setdefault("health_summary", "")
    result.setdefault("risk_drivers", [])
    result.setdefault("positive_signals", [])
    result.setdefault(
        "expansion_evidence",
        ["No current expansion evidence"],
    )
    result.setdefault("recommended_next_step", "")

    return result


def analyze_account(account):
    account_json = json.dumps(account, indent=2)

    prompt = f"""
You are a senior Customer Success Revenue Intelligence analyst.

Analyze the customer account using the combined CRM, product usage,
support, customer engagement, sentiment, and billing signals provided.

Your goal is to assess customer health, retention risk, and expansion
signals based only on the evidence provided.

Return ONLY valid raw JSON.
No markdown.
No explanation outside the JSON.

Use this exact structure:

{{
  "health_score": 0,
  "health_summary": "concise explanation of the customer's current condition",
  "churn_risk": "Low or Medium or High",
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

Guidance:

HEALTH SCORE
- Return a whole-number health score from 0 to 100.
- 80-100 = Healthy.
- 60-79 = Monitor.
- 40-59 = At Risk.
- 0-39 = Critical.
- Base the score on the combined evidence, not one individual signal.
- Avoid extreme scores below 25 or above 95 unless the evidence is unusually strong.

CHURN RISK
- Consider renewal timing, usage decline, feature adoption, support issues,
  customer sentiment, executive engagement, champion status, and billing.
- Do not assume churn from one negative signal alone.
- High churn risk should require multiple negative signals.
- Medium churn risk should indicate meaningful concern but not likely full churn.
- Low churn risk should indicate overall healthy retention conditions.

RISK DRIVERS
- Identify the most important negative signals contributing to the assessment.
- Every risk driver must be traceable to the provided account data.
- Be specific and quantify the signal whenever possible.
- Example: "Product logins declined 31% over 60 days" is better than
  "Usage is declining."

POSITIVE SIGNALS
- Identify evidence supporting retention, adoption, customer value, or stability.
- Every positive signal must be traceable to the provided account data.
- Include positive signals even for unhealthy accounts when they exist.

EXPANSION EVIDENCE
- Report expansion signals only when supported by the source data.
- Consider explicit expansion ARR, increasing usage, strong adoption,
  positive sentiment, stakeholder engagement, or other provided evidence.
- Clearly distinguish an identified commercial opportunity from general health.
- If there is no meaningful expansion evidence, say
  "No current expansion evidence."

ASSESSMENT BOUNDARIES
- Health Score and Churn Risk are analytical assessments, not financial forecasts.
- Do not calculate or estimate revenue loss.
- Do not invent contraction or expansion dollars.
- Do not infer facts that are not contained in the source data.

RECOMMENDED NEXT STEP
- Recommend one clear Customer Success action.
- The recommendation should reflect the evidence provided.
- Use professional Customer Success language.
- Avoid terms such as "emergency" unless the source data explicitly indicates
  a confirmed cancellation or non-renewal decision.
- For high-risk accounts, prefer language such as executive intervention,
  recovery plan, renewal risk review, or success plan.
- Human review and customer engagement remain the final decision points.

Customer Account Data:

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
        result = json.loads(cleaned)
        return normalize_analysis_result(account, result)

    except (json.JSONDecodeError, TypeError, ValueError) as error:
        print(
            f"Analysis parse failed for "
            f"{account['account_name']}: {error}"
        )
        print("Raw response:", raw_text)
        return None

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

    content = f"""# Customer Health Intelligence Audit — {account['account_name']}

## Executive Account Summary

| Metric | Current Position |
|---|---|
| Current ARR | ${account['arr']:,.0f} |
| Renewal Date | {format_date(account['renewal_date'])} |
| Days to Renewal | {account['days_to_renewal']} |
| Health Score | {result['health_score']}/100 |
| Health Status | {result['health_status']} |
| Churn Risk | {result['churn_risk']} |
| Revenue Exposure | ${result['revenue_exposure']:,.0f} |
| Known Contraction | ${result['known_contraction']:,.0f} |
| Known Expansion | ${result['known_expansion']:,.0f} |

### Health Summary

{result['health_summary']}

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

- **Monthly Active Users:** {usage['monthly_active_users']} of {usage['licensed_users']} licensed users
- **License Utilization:** {usage['license_utilization_pct']}%
- **60-Day Login Change:** {usage['login_change_60d_pct']}%
- **Core Feature Adoption:** {usage['core_feature_adoption_pct']}%
- **Automation Adoption:** {usage['automation_adoption_pct']}%
- **Usage Trend:** {usage['usage_trend']}

### Support

- **Total Tickets:** {support['ticket_count']}
- **High-Severity Tickets:** {support['high_severity_tickets']}
- **Open Tickets:** {support['open_tickets']}
- **Average Resolution Time:** {support['average_resolution_hours']} hours
- **Average CSAT:** {support['average_csat']}

### Customer Engagement

- **Days Since CSM Meeting:** {engagement['days_since_last_csm_meeting']}
- **Days Since Executive Meeting:** {engagement['days_since_last_exec_meeting']}
- **QBR Completed:** {engagement['qbr_completed']}
- **Champion Status:** {engagement['champion_status']}
- **Stakeholder Coverage:** {engagement['stakeholder_coverage']}
- **Open Action Items:** {engagement['open_action_items']}

### Customer Sentiment

- **Current NPS:** {sentiment['current_nps']}
- **Previous NPS:** {sentiment['previous_nps']}
- **NPS Change:** {sentiment['nps_change']} points
- **Current Sentiment:** {sentiment['sentiment']}
- **Customer Feedback:** {sentiment['primary_feedback']}

### Commercial Position

- **Current ARR:** ${account['arr']:,.0f}
- **Billing Status:** {billing['billing_status']}
- **Days Past Due:** {billing['days_past_due']}
- **Known Contraction:** ${billing['contraction_arr']:,.0f}
- **Known Expansion:** ${billing['expansion_arr']:,.0f}

---

## Recommended Next Step

{result['recommended_next_step']}

## Revenue Intelligence Definitions

- **Revenue Exposure:** Current ARR associated with an account assessed as having meaningful retention risk. This is not a prediction that this revenue will be lost.
- **Known Contraction:** Revenue reduction explicitly identified in the source data.
- **Known Expansion:** Additional recurring revenue explicitly identified in the source data.
- **Health Score:** AI assessment of the combined customer signals; it is not a financial forecast.
- **Health Status:** Determined from the Health Score using configured score bands.
- **Churn Risk:** Qualitative retention-risk assessment based on the combined customer signals.
- **Commercial Movement:** Known Contraction and Known Expansion are sourced directly from the client data, not generated by AI.
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
*⚠️ Churn Risk:* {result['churn_risk']}
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


def should_alert(account, result):
    alert_rules = HEALTH_RULES["alerts"]

    days_to_renewal = account.get("days_to_renewal")
    within_renewal_window = (
        days_to_renewal is not None
        and days_to_renewal
        <= alert_rules["high_risk_max_days_to_renewal"]
    )

    high_revenue_risk = (
        result["churn_risk"] == "High"
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
        and result["churn_risk"] == "Low"
    )

    return (
        high_revenue_risk
        or known_contraction_risk
        or material_expansion
    )


def get_priority_group(account, result):
    priority_rules = HEALTH_RULES["priority_routing"]

    if (
        result["churn_risk"]
        in set(priority_rules["executive_churn_risk"])
        or result["health_status"]
        in set(priority_rules["executive_health_status"])
        or (
            priority_rules["executive_on_known_contraction"]
            and result["known_contraction"] > 0
        )
    ):
        return "Executive Attention"

    if (
        result["churn_risk"]
        in set(priority_rules["csm_churn_risk"])
        or result["health_status"]
        in set(priority_rules["csm_health_status"])
    ):
        return "CSM Attention"

    if (
        result["known_expansion"] > 0
        and (
            not priority_rules["expansion_requires_low_churn"]
            or result["churn_risk"] == "Low"
        )
    ):
        return "Expansion"

    return "Healthy / No Immediate Action"

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
        if item["result"]["churn_risk"] == "High"
    )

    medium_risk_arr = sum(
        item["account"]["arr"]
        for item in portfolio_results
        if item["result"]["churn_risk"] == "Medium"
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
        "Expansion": [],
        "Healthy / No Immediate Action": [],
    }

    for item in portfolio_results:
        account = item["account"]
        result = item["result"]

        group = get_priority_group(
            account,
            result,
        )

        priority_groups[group].append(item)

    generated_date = datetime.now().strftime(
        "%m/%d/%Y %I:%M %p"
    )

    content = f"""# Customer Health Intelligence Audit — Portfolio Summary

**Generated:** {generated_date}

## Portfolio Snapshot

| Metric | Current Position |
|---|---:|
| Total Accounts | {total_accounts} |
| Total Portfolio ARR | ${total_arr:,.0f} |
| Healthy Accounts | {health_counts['Healthy']} |
| Monitor Accounts | {health_counts['Monitor']} |
| At-Risk Accounts | {health_counts['At Risk']} |
| Critical Accounts | {health_counts['Critical']} |

## Revenue Intelligence

| Metric | Current Position |
|---|---:|
| High-Risk ARR | ${high_risk_arr:,.0f} |
| Medium-Risk ARR | ${medium_risk_arr:,.0f} |
| Total Revenue Exposure | ${total_revenue_exposure:,.0f} |
| Known Contraction | ${total_contraction:,.0f} |
| Known Expansion | ${total_expansion:,.0f} |
| Net Known Revenue Movement | ${net_known_movement:,.0f} |

> Revenue Exposure represents current ARR associated with accounts showing meaningful retention risk. It is not predicted revenue loss and should be reviewed alongside the underlying account signals.

---

## Portfolio Priorities
"""

    for group_name in [
        "Executive Attention",
        "CSM Attention",
        "Expansion",
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
                f"- **Churn Risk:** {result['churn_risk']}\n"
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

            content += (
                f"- **Primary Signal:** "
                f"{summarize_reason(result)}\n"
                f"- **Recommended Action:** "
                f"{result['recommended_next_step']}\n\n"
            )

    content += """---

## How to Read This Report

- **Executive Attention:** Accounts with high churn risk, critical health, or known contraction requiring leadership visibility.
- **CSM Attention:** Accounts showing meaningful warning signals that require active management but may not yet require executive escalation.
- **Expansion:** Healthy accounts with explicit commercial expansion identified in the source data.
- **Healthy / No Immediate Action:** Accounts currently showing healthy retention conditions without a material identified expansion or contraction event.
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
    }


def print_portfolio_summary(summary, slack_alert_count):
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
        f"Expansion Opportunities:  "
        f"{len(summary['priority_groups']['Expansion'])}"
    )

    print(
        f"Executive Slack Alerts:   "
        f"{slack_alert_count}"
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
        ANTHROPIC_API_KEY is not None,
    )

    print(
        "SLACK WEBHOOK LOADED:",
        SLACK_WEBHOOK_URL is not None,
    )

    print(
        "\nConsolidating customer data "
        "from source systems..."
    )

    accounts = normalize_accounts()

    print(
        f"Created {len(accounts)} "
        "normalized account health records."
    )

    portfolio_results = []
    slack_alert_count = 0

    for account in accounts:
        print(
            f"\nAnalyzing: "
            f"{account['account_name']}"
        )

        result = analyze_account(account)

        if result is None:
            continue

        print(
            "Health:",
            f"{result['health_score']}/100 - "
            f"{result['health_status']}",
        )

        print(
            "Churn Risk:",
            result["churn_risk"],
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
        )


if __name__ == "__main__":
    main()