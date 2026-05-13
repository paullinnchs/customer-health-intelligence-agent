# Customer Health Intelligence Agent

Claude-powered Customer Success revenue intelligence workflow that analyzes customer health, churn risk, ARR exposure, and expansion opportunities.

---

## Overview

This project demonstrates how AI can support Customer Success and Revenue teams by converting account signals into structured revenue intelligence.

The workflow analyzes customer account data and produces:

- Health summary
- Churn risk level
- ARR at risk
- NRR opportunity
- Risk drivers
- Expansion opportunities
- Recommended next step
- Slack executive alert

---

## Business Objective

The goal is to help SaaS teams protect and expand recurring revenue by identifying risk and opportunity earlier.

This workflow is designed around core Customer Success metrics:

- Churn
- Net Revenue Retention (NRR)
- Annual Recurring Revenue (ARR)
- Health Score
- Time-to-Value

---

## Workflow Architecture

```text
accounts.csv
→ Claude API
→ Structured JSON revenue intelligence
→ Markdown account reports
→ Slack alerts for executive attention