# Customer Health Intelligence Agent

**A Paul Linn Solutions (PLS) customer-health workflow that consolidates account signals, assesses risk and opportunity, prioritizes attention, and produces portfolio-level revenue intelligence.**

This repository is the engine behind the **PLS Customer Health System**.

It is designed around a simple operating model:

**customer data → validation → normalization → deterministic health scoring → AI-assisted interpretation → deterministic routing logic → account reports → portfolio priorities → optional alerts**

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

Minimum commercial/account baseline, validated before processing:

- account_id
- account_name
- arr
- renewal_date

These four are enforced. A missing column, a blank value, a duplicate `account_id`, a non-numeric `arr`, or a date that is not `YYYY-MM-DD` fails the run with a message naming the file, row, account, and field. Dates in the optional sources are validated the same way, so a malformed export never surfaces as an unexplained traceback.

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

## The PLS Baseline Health Model

The Health Score is deterministic. The same data and the same configuration always produce the same score. Claude never generates it.

Five components, 97 configured points:

| Component | Points | Subcomponents |
|---|---:|---|
| Product Adoption & Usage | 30 | License utilization, 60-day login trend, core feature adoption, automation adoption |
| Customer Engagement | 20 | CSM cadence, executive engagement, QBR, champion status, stakeholder coverage, open action items |
| Customer Sentiment | 20 | Current NPS, NPS trend, qualitative sentiment |
| Support Health | 15 | High-severity tickets, open tickets, average CSAT, resolution performance |
| Commercial & Renewal Position | 12 | Known contraction, billing status, renewal proximity |

Two subcomponents are unavailable by design until they are calibrated for a client:

- **Resolution Performance (3 pts)** — resolution time is meaningless without a client benchmark, so there is deliberately no universal default. Until `bands` are configured it is unavailable.
- **Billing Status for overdue accounts (4 pts)** — `minor_overdue_max_days` is intentionally null. Accounts with `days_past_due > 0` are unavailable rather than scored against an invented threshold. Accounts at zero days past due score normally.

The approved model originally specified 15 points for Commercial & Renewal Position, including a 3-point Renewal + Risk Interaction subcomponent. The schema has no field identifying "no material concern / meaningful concern / confirmed non-renewal", and that subcomponent was circular because it fed the score that Retention Risk is derived from. It was dropped by explicit approval, reducing Commercial to 12 points and the configured total to 97.

### Missing signals never score zero

Each subcomponent reports whether it was available. The score is normalized against the points that were actually available:

```text
Health Score    = points_earned / points_available * 100
Signal Coverage = points_available / points_configured * 100
```

An account with no NPS data is not penalised for it; the sentiment points simply leave both the numerator and the denominator, and Signal Coverage drops so the gap is visible.

---

## What Is Deterministic vs. AI-Assisted

### Deterministic

`health_model.py` and `main.py` handle:

- Component and subcomponent scoring
- Composite Health Score
- Health Status from configured score bands
- Retention Risk from Health Status
- Signal Coverage
- Revenue Exposure
- Known Contraction and Known Expansion from source data
- Expansion Opportunity identification
- Executive / CSM routing
- Alert thresholds
- Portfolio aggregation

### AI-Assisted

Claude receives the deterministic results as context and returns interpretation only:

- Health Summary
- Risk Drivers
- Positive Signals
- Expansion Evidence
- Recommended Next Step

The prompt explicitly forbids the model from producing a score, a status, a risk level, or any dollar figure, and from contradicting the deterministic classifications. Every statement must trace back to the supplied account data or the scoring results.

If narrative generation fails for an account, the deterministic results still stand and the account remains in the portfolio. Only the narrative is marked unavailable.

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

Deterministic PLS Baseline Health Model result. Points earned across available signals, normalized to 100.

It is not AI-generated and it is not a financial forecast.

### Health Status

Derived deterministically from the Health Score using the configured score bands.

### Retention Risk

Derived deterministically from Health Status, replacing the previous AI-generated Churn Risk:

| Health Status | Retention Risk |
|---|---|
| Healthy | Low |
| Monitor | Medium |
| At Risk | Medium |
| Critical | High |

### Signal Coverage

The share of the configured scoring model that an account's available data supported.

This is a measure of data completeness. It is **not** statistical confidence.

---

## Portfolio Priorities

Each account is routed into exactly one primary attention group:

### Executive Attention

High retention risk, Critical health, or known contraction requiring leadership visibility.

### CSM Attention

Meaningful warning signals requiring active Customer Success management. Monitor and At Risk accounts land here — Monitor is a Health Status, not an attention group.

### Healthy / No Immediate Action

Accounts currently showing healthy retention conditions.

Routing rules are configurable in:

`config/health_rules.yaml`

### Expansion Opportunity is separate

Expansion is **not** an attention group. It is evaluated independently from Known Expansion in the source data, so an account can simultaneously be Executive Attention and an Expansion Opportunity. Expansion never improves or masks the Health Score.

---

## Configurable Rules

`config/health_rules.yaml` controls:

- PLS Baseline Status Bands
- Retention Risk mapping
- Every component and subcomponent scoring band in the PLS Baseline Health Model
- Champion status and stakeholder coverage vocabulary
- The client resolution-performance benchmark
- The minor / materially overdue billing boundary
- Minimum ARR for high-risk executive alerts
- Renewal-window threshold
- Known-contraction alert threshold
- Known-expansion alert threshold
- Whether expansion alerting is gated on Low retention risk
- Executive / CSM routing

This allows the model to be calibrated for a client portfolio without rewriting Python.

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
- Retention Risk
- Signal Coverage
- Revenue Exposure
- Known Contraction
- Known Expansion
- Expansion Opportunity
- Health Score Breakdown — points earned / points available per component
- Which signals were unavailable
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
- Healthy / No Immediate Action
- Expansion Opportunities, counted independently of attention routing

### Optional Slack Alerts

Slack alerts are sent only when configured executive thresholds are met.

Alert counts are internal operational logging. They are deliberately not a customer-facing portfolio intelligence metric.

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
