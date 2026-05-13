# Customer Health Intelligence Agent

Claude-powered Customer Success revenue intelligence workflow that analyzes customer health, churn risk, ARR exposure, estimated NRR impact, and expansion opportunities.

---

## Overview

This project demonstrates how AI can support Customer Success and Revenue teams by converting account signals into structured revenue intelligence.

The workflow analyzes customer account data and produces:

- Health Summary
- Churn Risk
- ARR at Risk
- Estimated NRR Impact
- Expansion Opportunity Level
- Risk Drivers
- Expansion Opportunities
- Recommended Next Step
- Slack Executive Alert

---

## Business Objective

The goal is to help SaaS teams protect and expand recurring revenue by identifying risk and opportunity earlier.

This workflow is designed around the core Customer Success and revenue metrics that leaders track:

- Churn
- Net Revenue Retention (NRR)
- Annual Recurring Revenue (ARR)
- Health Score
- Time-to-Value

---

## Revenue Metrics Modeled

This workflow is designed around the revenue metrics Customer Success leaders care about most:

- **Churn Risk** — Likelihood an account may contract or churn.
- **ARR at Risk** — Revenue exposed if the account contracts or churns.
- **Estimated NRR Impact** — Modeled retention and expansion percentage for the account.
- **Expansion Opportunity Level** — Qualitative signal indicating upsell potential.
- **Health Score** — Current account condition based on operational signals.
- **Time-to-Value** — Speed at which the customer reaches measurable value.

---

## Workflow Architecture

```text
accounts.csv
→ Claude API
→ Structured JSON revenue intelligence
→ Markdown account reports
→ Slack alerts for executive attention