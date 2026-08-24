# Customer Health Intelligence Agent

**A Paul Linn Solutions (PLS) customer-health workflow that consolidates account signals, assesses risk and opportunity, prioritizes attention, and produces portfolio-level revenue intelligence.**

This repository is the engine behind the **PLS Customer Health System**.

It is designed around a simple operating model:

**customer data → normalization → AI-assisted health analysis → deterministic revenue/routing logic → account reports → portfolio priorities → optional alerts**

---

## What This Solves

Customer-health signals often live across multiple systems:

- CRM / account records
- Product usage
- Support
- Customer engagement
- NPS / sentiment
- Billing / commercial data

Looking at any one source in isolation can hide meaningful risk or opportunity.

This workflow consolidates those signals into one account-level record and answers:

- Which accounts need Executive Attention?
- Which accounts need CSM Attention?
- Which accounts are healthy?
- Where is there explicit expansion?
- What revenue is associated with accounts showing meaningful retention risk?
- What specific signals caused the assessment?
- What should happen next?

---

## Standard Customer Inputs

### Required

`crm_accounts.csv`

Minimum commercial/account baseline:

- account_id
- account_name
- arr
- renewal_date

The sample schema also includes fields such as segment, CSM, industry, employee count, contract start, and account stage.

### Optional Health-Signal Sources

The engine can use any of the following when available:

- `product_usage.csv`
- `support_tickets.csv`
- `customer_engagement.csv`
- `customer_sentiment.csv`
- `billing.csv`

Missing optional sources are represented explicitly as unavailable rather than silently treated as healthy zero values.

The included `sample_data/` files demonstrate the expected schema.

---

## Workflow Architecture

```text
CRM Account Data ───────────────┐
Product Usage ─────────────────┤
Support Tickets ───────────────┤
Customer Engagement ───────────┤
Customer Sentiment ────────────┤
Billing / Commercial Data ─────┘
               ↓
        normalize_data.py
               ↓
     Normalized Account Record
               ↓
      AI Health Assessment
               ↓
  Deterministic Revenue + Routing
               ↓
    Account Intelligence Reports
               ↓
       Portfolio Summary
               ↓
   Optional Executive Slack Alerts
```

---

## What Is AI-Assisted vs. Deterministic

### AI-Assisted

Claude evaluates the combined customer signals and returns:

- Health Score
- Churn Risk
- Health Summary
- Risk Drivers
- Positive Signals
- Expansion Evidence
- Recommended Next Step

These are analytical assessments based on the supplied account evidence.

### Deterministic

Python handles:

- Health Status from configured score bands
- Revenue Exposure calculation
- Known Contraction from source data
- Known Expansion from source data
- Executive / CSM / Expansion routing
- Alert thresholds
- Portfolio aggregation

This intentionally prevents the model from inventing commercial dollar values or deciding routing thresholds on its own.

---

## Revenue Intelligence Definitions

### Revenue Exposure

Current ARR associated with an account assessed as having meaningful retention risk.

It is **not predicted revenue loss**.

- High churn risk → current ARR is exposed
- Medium churn risk → current ARR is exposed for monitoring
- Low churn risk → $0 revenue exposure

### Known Contraction

Revenue reduction explicitly present in the client/source data.

It is not estimated by AI.

### Known Expansion

Additional recurring revenue explicitly present in the client/source data.

It is not estimated by AI.

### Health Score

AI-assisted assessment of the combined account signals.

It is not a financial forecast.

### Health Status

Derived deterministically from the Health Score using the configured score bands.

---

## Portfolio Priorities

Each account is routed into one primary operating group:

### Executive Attention

High churn risk, Critical health, or known contraction requiring leadership visibility.

### CSM Attention

Meaningful warning signals requiring active Customer Success management.

### Expansion

Healthy/low-risk accounts with explicit known expansion.

### Healthy / No Immediate Action

Accounts currently showing healthy retention conditions without a material known expansion or contraction event.

Routing rules are configurable in:

`config/health_rules.yaml`

---

## Configurable Rules

`config/health_rules.yaml` controls:

- Health-score bands
- Minimum ARR for high-risk executive alerts
- Renewal-window threshold
- Known-contraction alert threshold
- Known-expansion alert threshold
- Executive / CSM / Expansion routing

This allows the workflow to fit different portfolio sizes without rewriting `main.py`.

---

## Outputs

Running the workflow creates:

### Normalized Data

`outputs/normalized_accounts.json`

### Account Reports

One Markdown report per analyzed account.

Each report includes:

- Account snapshot
- Health Score / Status
- Churn Risk
- Revenue Exposure
- Known Contraction
- Known Expansion
- Risk Drivers
- Positive Signals
- Expansion Evidence
- Underlying health signals
- Recommended Next Step

### Portfolio Report

`outputs/portfolio-health-summary.md`

The portfolio summary includes:

- Accounts analyzed
- Total portfolio ARR
- Healthy / Monitor / At Risk / Critical counts
- High-Risk ARR
- Medium-Risk ARR
- Revenue Exposure
- Known Contraction
- Known Expansion
- Net Known Revenue Movement
- Executive Attention
- CSM Attention
- Expansion Opportunities

### Optional Slack Alerts

Slack alerts are sent only when configured executive thresholds are met.

---

## Data Handling

The public repository includes only sample/mock data.

Never commit customer exports or customer-specific outputs.

For local client work, place files in a private folder such as:

```text
client_data/
```

Then set:

```text
CUSTOMER_HEALTH_DATA_DIR=client_data
```

in your local `.env`.

`client_data/` and `outputs/` are excluded from Git.

---

## Local Setup

From the repository root:

```bash
uv venv
```

Activate the environment on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
uv pip install -r requirements.txt
```

Create a local `.env`:

```text
ANTHROPIC_API_KEY=your_key_here
SLACK_WEBHOOK_URL=optional_slack_webhook
CUSTOMER_HEALTH_DATA_DIR=sample_data
```

Run the normalization layer by itself:

```bash
uv run python normalize_data.py
```

Run the complete workflow:

```bash
uv run python main.py
```

---

## Current Prototype Boundary

This repository demonstrates the working analysis and prioritization engine.

The initial PLS Customer Health engagement can operate against customer-provided exports without requiring production integrations.

If the workflow proves valuable, the same engine can then be connected to the customer's existing systems for scheduled monitoring, reporting, and alerts.

---

## Commercial Delivery Model

```text
DEFINE
Confirm the Customer Health use case, available signals, and portfolio thresholds.

RUN
Provide existing customer-health data and execute the intelligence workflow.

AUTOMATE
Connect the proven workflow to existing systems for ongoing monitoring and alerts.
```

---

*Built by Paul Linn Solutions — practical operational systems for customer operations, recruiting, and workforce technology.*
