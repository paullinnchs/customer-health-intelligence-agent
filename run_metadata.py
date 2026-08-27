"""
run_metadata.py
---------------
The record of what a client run actually did.

A delivered run has to be defensible six months later, when the client asks why
an account scored what it scored. That requires knowing the inputs, the exact
configuration, the effective date, and the engine version — not just the
outputs. This module captures all of it into run_manifest.json, plus a
human-readable run-summary.md for the delivery folder.

Reproducing a run means: same input CSVs, same baseline, same overrides, and
`--as-of <effective_date>` from the manifest.
"""

import hashlib
import platform
import sys
from datetime import datetime
from pathlib import Path

from client_config import file_sha256


REPO_ROOT = Path(__file__).resolve().parent

# Hashed so a manifest identifies the engine that produced it. health_model.py
# is the locked scoring engine and is the one that matters most.
ENGINE_FILES = (
    "health_model.py",
    "normalize_data.py",
    "main.py",
    "client_config.py",
    "client_workspace.py",
)

MANIFEST_FILENAME = "run_manifest.json"
SUMMARY_FILENAME = "run-summary.md"
RESOLVED_CONFIG_FILENAME = "resolved_health_rules.yaml"


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _engine_fingerprint():
    fingerprint = {}

    for name in ENGINE_FILES:
        path = REPO_ROOT / name

        if path.exists():
            fingerprint[name] = file_sha256(path)

    return fingerprint


def _input_fingerprint(workspace):
    """Hash every CSV actually present in the client's input directory."""
    files = {}

    for path in sorted(workspace.input_dir.glob("*.csv")):
        files[path.name] = {
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
        }

    return files


def _output_fingerprint(workspace, extra_names=()):
    files = []

    for path in workspace.written_files:
        files.append(
            {
                "file": str(path.relative_to(workspace.root)).replace(
                    "\\", "/"
                ),
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
        )

    for name in extra_names:
        files.append(
            {
                "file": f"outputs/{name}",
                "sha256": None,
                "bytes": None,
                "note": "written after the manifest was assembled",
            }
        )

    return files


def build(
    workspace,
    client,
    baseline_path,
    resolved_config_text,
    scored,
    portfolio,
    warnings,
    warning_summary,
    started_at,
    effective_date,
    flags,
    model,
    narrative_failures,
    alerts_sent,
    alerts_suppressed,
    validation_status,
):
    """Assemble the manifest payload. Pure data; no I/O."""
    coverages = [
        item["result"]["signal_coverage_pct"] for item in scored
    ]

    finished_at = datetime.now()

    return {
        "schema": "chia.run_manifest/1",
        "client": {
            "name": client["name"],
            "slug": client["slug"],
            "engagement": client.get("engagement"),
            "workspace": workspace.display_root(),
        },
        "run": {
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round(
                (finished_at - started_at).total_seconds(), 1
            ),
            "effective_date": effective_date.strftime("%Y-%m-%d"),
            "effective_date_source": flags["as_of_source"],
            "command": flags["command"],
            "flags": {
                "validate_only": flags["validate_only"],
                "no_ai": flags["no_ai"],
                "no_slack": flags["no_slack"],
                "as_of": flags["as_of"],
            },
            "engine": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "narrative_model": model,
                "file_sha256": _engine_fingerprint(),
            },
        },
        "configuration": {
            "baseline_path": str(
                Path(baseline_path).relative_to(REPO_ROOT)
            ).replace("\\", "/"),
            "baseline_sha256": file_sha256(baseline_path),
            "client_config_path": str(
                workspace.config_path.relative_to(workspace.root)
            ).replace("\\", "/"),
            "client_config_sha256": file_sha256(workspace.config_path),
            "resolved_config_file": RESOLVED_CONFIG_FILENAME,
            "resolved_config_sha256": _sha256_text(resolved_config_text),
            "points_configured": (
                scored[0]["result"]["points_configured"] if scored else None
            ),
            "overrides_applied": len(client["overrides"]),
            "overrides": [
                {
                    "path": entry["path"],
                    "baseline_value": entry["baseline_value"],
                    "client_value": entry["client_value"],
                    "rationale": entry["rationale"],
                    "permitted_because": entry["permitted_because"],
                }
                for entry in client["overrides"]
            ],
        },
        "inputs": {
            "input_dir": str(
                workspace.input_dir.relative_to(workspace.root)
            ).replace("\\", "/"),
            "files": _input_fingerprint(workspace),
        },
        "portfolio": {
            "account_count": len(scored),
            "total_arr": portfolio.get("total_arr"),
            "signal_coverage": {
                "average_pct": (
                    round(sum(coverages) / len(coverages), 1)
                    if coverages
                    else None
                ),
                "minimum_pct": min(coverages) if coverages else None,
                "maximum_pct": max(coverages) if coverages else None,
                "accounts_at_full_coverage": sum(
                    1 for pct in coverages if pct == 100
                ),
            },
            "health_counts": portfolio.get("health_counts"),
            "revenue_exposure": portfolio.get("revenue_exposure"),
            "known_contraction": portfolio.get("known_contraction"),
            "known_expansion": portfolio.get("known_expansion"),
            "net_known_movement": portfolio.get("net_known_movement"),
            "attention_groups": {
                name: len(items)
                for name, items in (
                    portfolio.get("priority_groups") or {}
                ).items()
            },
            "expansion_opportunities": len(
                portfolio.get("expansion_opportunities") or []
            ),
        },
        "accounts": [
            {
                "account_id": item["account"]["account_id"],
                "account_name": item["account"]["account_name"],
                "arr": item["account"]["arr"],
                "health_score": item["result"]["health_score"],
                "health_status": item["result"]["health_status"],
                "retention_risk": item["result"]["retention_risk"],
                "signal_coverage_pct": item["result"]["signal_coverage_pct"],
                "points_earned": item["result"]["points_earned"],
                "points_available": item["result"]["points_available"],
                "revenue_exposure": item["result"]["revenue_exposure"],
                "narrative_available": item["result"].get(
                    "narrative_available", True
                ),
            }
            for item in scored
        ],
        "validation": {
            "status": validation_status,
            "required_fields_checked": [
                "account_id",
                "account_name",
                "arr",
                "renewal_date",
            ],
            "date_format": "YYYY-MM-DD",
        },
        "data_quality": {
            "summary": warning_summary,
            "conditions": warnings,
        },
        "operations": {
            "narrative_failures": narrative_failures,
            "slack_alerts_sent": alerts_sent,
            "slack_alerts_suppressed": alerts_suppressed,
        },
        "outputs": {"files": []},
    }


def write(workspace, manifest):
    """
    Write the run summary, then the manifest.

    The manifest records every file the run produced, so it is written last and
    lists itself explicitly.
    """
    workspace.write_text(SUMMARY_FILENAME, render_summary(manifest))

    manifest["outputs"]["files"] = _output_fingerprint(
        workspace, extra_names=[MANIFEST_FILENAME]
    )
    manifest["outputs"]["count"] = len(manifest["outputs"]["files"])

    return workspace.write_json(MANIFEST_FILENAME, manifest)


def render_summary(manifest):
    """A short, client-readable companion to the JSON manifest."""
    client = manifest["client"]
    run = manifest["run"]
    config = manifest["configuration"]
    portfolio = manifest["portfolio"]
    coverage = portfolio["signal_coverage"]

    lines = [
        f"# Run Summary — {client['name']}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Client | {client['name']} |",
        f"| Client Workspace | `{client['workspace']}` |",
    ]

    if client.get("engagement"):
        lines.append(f"| Engagement | {client['engagement']} |")

    lines.extend(
        [
            f"| Run Started | {run['started_at']} |",
            f"| Run Finished | {run['finished_at']} |",
            f"| Effective Date | {run['effective_date']} "
            f"({run['effective_date_source']}) |",
            f"| Validation | {manifest['validation']['status']} |",
            f"| Accounts Analyzed | {portfolio['account_count']} |",
            f"| Portfolio ARR | "
            f"{_money(portfolio['total_arr'])} |",
            f"| Average Signal Coverage | "
            f"{_pct(coverage['average_pct'])} |",
            f"| Signal Coverage Range | "
            f"{_pct(coverage['minimum_pct'])}–"
            f"{_pct(coverage['maximum_pct'])} |",
            f"| Configuration | `{config['baseline_path']}` |",
            f"| Baseline SHA-256 | `{config['baseline_sha256'][:16]}…` |",
            f"| Overrides Applied | {config['overrides_applied']} |",
            f"| Data-Quality Conditions | "
            f"{manifest['data_quality']['summary']['total']} "
            f"({manifest['data_quality']['summary']['warnings']} warning, "
            f"{manifest['data_quality']['summary']['info']} info) |",
            "",
            "## Configuration",
            "",
        ]
    )

    if not config["overrides"]:
        lines.append(
            "This run used the PLS baseline health configuration with no "
            "client overrides."
        )
    else:
        lines.append(
            "This run inherited the PLS baseline health configuration and "
            "applied the following documented client overrides."
        )
        lines.append("")

        for entry in config["overrides"]:
            lines.extend(
                [
                    f"#### `{entry['path']}`",
                    "",
                    f"- **PLS baseline value:** `{entry['baseline_value']}`",
                    f"- **Client value:** `{entry['client_value']}`",
                    f"- **Rationale:** {entry['rationale']}",
                    f"- **Permitted because:** {entry['permitted_because']}",
                    "",
                ]
            )

    lines.extend(["", "## Data-Quality Conditions", ""])

    conditions = manifest["data_quality"]["conditions"]

    if not conditions:
        lines.append("No data-quality conditions were detected in this run.")
    else:
        for finding in conditions:
            tag = (
                "**Warning**"
                if finding["severity"] == "warning"
                else "Information"
            )
            lines.append(f"- {tag} — {finding['message']}")

            if finding.get("accounts"):
                lines.append(
                    f"  - Accounts: {', '.join(finding['accounts'])}"
                )

    lines.extend(
        [
            "",
            "## Reproducing This Run",
            "",
            "```bash",
            f"uv run python main.py {client['workspace']} "
            f"--as-of {run['effective_date']}",
            "```",
            "",
            "The effective date is pinned because `days_to_renewal` is "
            "measured from it, and that value feeds Renewal Proximity and the "
            "alert renewal window. Re-running with the same inputs, the same "
            "configuration, and the same effective date reproduces every "
            "deterministic result in this delivery.",
            "",
            "Full detail, including input and output file hashes, is in "
            f"`{MANIFEST_FILENAME}`.",
            "",
        ]
    )

    return "\n".join(lines)


def _money(value):
    if value is None:
        return "Not available"

    return f"${value:,.0f}"


def _pct(value):
    if value is None:
        return "Not available"

    return f"{value}%"
