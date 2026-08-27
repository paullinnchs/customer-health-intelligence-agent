# Client Workspaces

Each subdirectory here is one client engagement. A CHIA run serves exactly one
of them, and every file it writes lands in that client's `outputs/` directory.

```text
clients/
  acme/
    config/client_config.yaml     tracked   — inherits the PLS baseline
    input/*.csv                   IGNORED   — the client's exports
    outputs/                      IGNORED   — the delivery
```

## Standing up a new client

```bash
cp -r clients/_template clients/acme
```

Then:

1. Edit `clients/acme/config/client_config.yaml`. Set `client.name` and set
   `client.slug` to `acme` — it must match the directory name, and a mismatch
   stops the run because it usually means the file was copied from another
   client.
2. Replace the header-only CSVs in `clients/acme/input/` with the client's
   exports. `crm_accounts.csv` is the only mandatory one.
3. Validate before you run:

   ```bash
   uv run python main.py clients/acme --validate-only
   ```

4. Produce the delivery:

   ```bash
   uv run python main.py clients/acme
   ```

## What a client may change

The PLS Baseline Health Model in `config/health_rules.yaml` is the methodology
and it is locked. Scoring weights, score bands, Retention Risk, Signal
Coverage, Revenue Exposure, expansion logic, and priority routing are not
client-configurable.

A client configuration may override exactly seven paths, each requiring a
written rationale:

| Path | What it is |
|---|---|
| `health_model.support_health.average_resolution_hours.bands` | The client's resolution-time benchmark. Ships uncalibrated. |
| `health_model.support_health.average_resolution_hours.above_points` | Companion to the above. |
| `health_model.commercial_renewal.billing_status.minor_overdue_max_days` | The minor / materially overdue boundary. Ships as `null`. |
| `alerts.high_risk_min_arr` | Slack routing threshold. |
| `alerts.high_risk_max_days_to_renewal` | Slack routing threshold. |
| `alerts.known_contraction_min_arr` | Slack routing threshold. |
| `alerts.known_expansion_min_arr` | Slack routing threshold. |

Anything else stops the run. An unrecognized path would require inventing a new
scoring rule; a recognized but locked path would change the methodology. Both
need a written methodology change, not a configuration edit.

## Data handling

`clients/*/input/` and `clients/*/outputs/` are excluded from Git. Client
configuration is tracked so a delivery can be reproduced, which is why the
Slack webhook secret is never allowed in it — the configuration names an
environment variable and the value lives in `.env`.

`clients/_template/` is tracked in full. Its CSVs are headers only and contain
no customer data.

## Reproducing a delivered run

Every run writes `outputs/run_manifest.json`, which records the input file
hashes, the resolved configuration, the overrides and their rationales, and the
effective date. To reproduce:

```bash
uv run python main.py clients/acme --as-of <effective_date from the manifest>
```

The effective date has to be pinned because `days_to_renewal` is measured from
it, and that feeds Renewal Proximity and the alert renewal window.
