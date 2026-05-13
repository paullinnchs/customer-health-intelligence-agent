import csv
import json
import os
import re
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

MODEL = "claude-haiku-4-5-20251001"
OUTPUT_DIR = Path("outputs")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def clean_json_text(text):
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def analyze_account(row):
    prompt = f"""
You are a senior Customer Success Revenue Intelligence analyst.

Analyze the customer account and return structured revenue intelligence.

Return ONLY valid raw JSON.
No markdown. No explanation.

Use this exact structure:
{{
  "health_summary": "short customer health summary",
  "churn_risk": "Low or Medium or High",
  "arr_at_risk": 0,
  "estimated_nrr_impact": 100,
  "expansion_opportunity_level": "Low or Medium or High",
  "risk_drivers": ["driver 1", "driver 2"],
  "expansion_opportunities": ["opportunity 1", "opportunity 2"],
  "recommended_next_step": "specific next action",
  "executive_alert": true
}}

Guidance:
- estimated_nrr_impact should be a realistic percentage.
- Below 100 means contraction or churn risk.
- 100 means flat retention.
- 105-110 means healthy expansion potential.
- 110+ means strong expansion potential.

Account Data:
Account Name: {row["account_name"]}
ARR: {row["arr"]}
Renewal Date: {row["renewal_date"]}
Health Score: {row["health_score"]}
Usage Trend: {row["usage_trend"]}
Open Tickets: {row["open_tickets"]}
NPS Score: {row["nps_score"]}
Last Activity Days: {row["last_activity_days"]}
Expansion Signal: {row["expansion_signal"]}
Time to Value Days: {row["time_to_value_days"]}
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = response.content[0].text
    cleaned = clean_json_text(raw_text)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as error:
        print(f"JSON parse failed for {row['account_name']}: {error}")
        print("Raw response:", raw_text)
        return None


def save_markdown_report(row, result):
    OUTPUT_DIR.mkdir(exist_ok=True)

    filename = f"{row['account_name'].lower().replace(' ', '-')}-health-intelligence.md"
    path = OUTPUT_DIR / filename

    content = f"""# Customer Health Intelligence Report — {row['account_name']}

## Account Snapshot

- **ARR:** ${row['arr']}
- **Renewal Date:** {row['renewal_date']}
- **Health Score:** {row['health_score']}
- **Usage Trend:** {row['usage_trend']}
- **Open Tickets:** {row['open_tickets']}
- **NPS Score:** {row['nps_score']}
- **Last Activity Days:** {row['last_activity_days']}
- **Expansion Signal:** {row['expansion_signal']}
- **Time to Value Days:** {row['time_to_value_days']}

## Health Summary

{result['health_summary']}

## Churn Risk

{result['churn_risk']}

## ARR at Risk

${result['arr_at_risk']}

## Estimated NRR Impact

{result['estimated_nrr_impact']}%

## Expansion Opportunity Level

{result['expansion_opportunity_level']}

## Risk Drivers

{chr(10).join(f"- {driver}" for driver in result['risk_drivers'])}

## Expansion Opportunities

{chr(10).join(f"- {opportunity}" for opportunity in result['expansion_opportunities'])}

## Recommended Next Step

{result['recommended_next_step']}
"""

    path.write_text(content, encoding="utf-8")
    print(f"Saved report: {path}")


def send_slack_alert(row, result):
    if not SLACK_WEBHOOK_URL:
        print("Slack webhook missing. Skipping alert.")
        return

    message = f"""
🚨 *Customer Health Intelligence Alert*

*Account:* {row['account_name']}
*ARR:* ${row['arr']}
*Renewal Date:* {row['renewal_date']}

━━━━━━━━━━

*⚠️ Churn Risk:* {result['churn_risk']}
*💰 ARR at Risk:* ${result['arr_at_risk']}
*📊 Estimated NRR Impact:* {result['estimated_nrr_impact']}%
*📈 Expansion Opportunity:* {result['expansion_opportunity_level']}

*Health Summary*
{result['health_summary']}

*Recommended Next Step*
{result['recommended_next_step']}

━━━━━━━━━━
"""

    response = requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=10)

    if response.status_code == 200:
        print(f"Slack alert sent for {row['account_name']}.")
    else:
        print("Slack error:", response.status_code, response.text)


def should_alert(result):
    return (
        result["executive_alert"] is True
        or result["churn_risk"] == "High"
        or result["arr_at_risk"] >= 50000
        or result["estimated_nrr_impact"] < 100
        or result["expansion_opportunity_level"] == "High"
    )


def main():
    print("ANTHROPIC KEY LOADED:", ANTHROPIC_API_KEY is not None)
    print("SLACK WEBHOOK LOADED:", SLACK_WEBHOOK_URL is not None)

    with open("accounts.csv", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            print(f"\nAnalyzing: {row['account_name']}")
            result = analyze_account(row)

            if result is None:
                continue

            print("Churn Risk:", result["churn_risk"])
            print("ARR at Risk:", result["arr_at_risk"])
            print("Estimated NRR Impact:", f"{result['estimated_nrr_impact']}%")
            print("Expansion Opportunity:", result["expansion_opportunity_level"])

            save_markdown_report(row, result)

            if should_alert(result):
                send_slack_alert(row, result)


if __name__ == "__main__":
    main()