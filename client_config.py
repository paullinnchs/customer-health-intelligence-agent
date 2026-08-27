"""
client_config.py
----------------
PLS baseline inheritance and controlled client overrides.

The PLS Baseline Health Model lives in config/health_rules.yaml and is the
single source of the scoring methodology. A client workspace inherits it
whole. A client may then override a short, explicit allowlist of paths, and
nothing else.

The allowlist exists because two subcomponents in the baseline are deliberately
uncalibrated — they are documented in health_rules.yaml as requiring a
client-specific benchmark before they can score anything — and because Slack
alert thresholds are operational routing rather than methodology.

Everything else is locked. An override naming a path that does not already
exist in the baseline is rejected as an attempt to invent a new scoring rule.
An override naming a path that exists but is locked is rejected as a
methodology change. Both are STOP conditions.
"""

import copy
import hashlib
from pathlib import Path

import yaml

from chia_errors import ConfigError, OverrideError


REPO_ROOT = Path(__file__).resolve().parent
CANONICAL_BASELINE = REPO_ROOT / "config" / "health_rules.yaml"

TOP_LEVEL_KEYS = {"client", "baseline", "slack", "overrides", "notes"}
CLIENT_KEYS = {"name", "slug", "engagement", "contact", "notes"}
SLACK_KEYS = {"enabled", "webhook_env_var"}
OVERRIDE_KEYS = {"path", "value", "rationale"}


# ---------------------------------------------------------------------------
# Override allowlist
# ---------------------------------------------------------------------------
# Only these paths may be overridden by a client. Each entry names the
# validator that guards the supplied value and the reason the path is open.
#
# Adding a path here is a methodology decision, not a code change. Do not
# extend this table without written approval.

def _validate_resolution_bands(value, baseline_rules):
    """
    Resolution Performance bands (3 points, empty in the baseline).

    health_rules.yaml documents this as the one place a client resolution
    benchmark is supplied. The bands must stay inside the 3 points already
    configured for the subcomponent, so calibrating it changes what the client
    can earn but never what the model is worth.
    """
    max_points = baseline_rules["health_model"]["support_health"][
        "average_resolution_hours"
    ]["max_points"]

    if not isinstance(value, list) or not value:
        raise OverrideError(
            "Resolution Performance bands must be a non-empty list of "
            "{ at_most: <hours>, points: <points> } entries.",
            "See the worked example in the average_resolution_hours block of "
            "config/health_rules.yaml.",
        )

    previous_at_most = None
    previous_points = None

    for index, band in enumerate(value, start=1):
        if not isinstance(band, dict) or set(band) != {"at_most", "points"}:
            raise OverrideError(
                f"Resolution Performance band {index} must have exactly the "
                f"keys 'at_most' and 'points'; got {band!r}.",
                "Match the format shown in config/health_rules.yaml.",
            )

        at_most = band["at_most"]
        points = band["points"]

        if not isinstance(at_most, (int, float)) or isinstance(at_most, bool):
            raise OverrideError(
                f"Resolution Performance band {index}: 'at_most' must be a "
                f"number of hours; got {at_most!r}.",
                "Use a plain number, for example 24.",
            )

        if at_most <= 0:
            raise OverrideError(
                f"Resolution Performance band {index}: 'at_most' must be "
                f"greater than 0 hours; got {at_most!r}.",
                "A non-positive resolution time cannot be met by any ticket.",
            )

        if not isinstance(points, int) or isinstance(points, bool):
            raise OverrideError(
                f"Resolution Performance band {index}: 'points' must be a "
                f"whole number; got {points!r}.",
                "Use an integer between 0 and "
                f"{max_points}.",
            )

        if points < 0 or points > max_points:
            raise OverrideError(
                f"Resolution Performance band {index}: 'points' must be "
                f"between 0 and {max_points}; got {points}.",
                f"Resolution Performance is configured as a {max_points}-point "
                f"subcomponent. Changing its weight is a methodology change "
                f"and is not permitted as a client override.",
            )

        # The model takes the first band the value is at or under, so an
        # out-of-order list would silently award the wrong tier.
        if previous_at_most is not None and at_most <= previous_at_most:
            raise OverrideError(
                f"Resolution Performance bands must be listed in ascending "
                f"'at_most' order; band {index} ({at_most}) is not greater "
                f"than the previous band ({previous_at_most}).",
                "Reorder the bands fastest-first, for example 24, 48, 72.",
            )

        if previous_points is not None and points > previous_points:
            raise OverrideError(
                f"Resolution Performance bands must not award more points for "
                f"a slower resolution time; band {index} awards {points} "
                f"after a band awarding {previous_points}.",
                "Faster resolution must be worth at least as much as slower "
                "resolution.",
            )

        previous_at_most = at_most
        previous_points = points


def _validate_above_points(value, baseline_rules):
    max_points = baseline_rules["health_model"]["support_health"][
        "average_resolution_hours"
    ]["max_points"]

    if not isinstance(value, int) or isinstance(value, bool):
        raise OverrideError(
            f"Resolution Performance 'above_points' must be a whole number; "
            f"got {value!r}.",
            f"Use an integer between 0 and {max_points}.",
        )

    if value < 0 or value > max_points:
        raise OverrideError(
            f"Resolution Performance 'above_points' must be between 0 and "
            f"{max_points}; got {value}.",
            "Changing the weight of the subcomponent is a methodology change "
            "and is not permitted as a client override.",
        )


def _validate_minor_overdue_days(value, baseline_rules):
    if not isinstance(value, int) or isinstance(value, bool):
        raise OverrideError(
            f"'minor_overdue_max_days' must be a whole number of days; got "
            f"{value!r}.",
            "Use an integer, for example 30.",
        )

    if value < 1:
        raise OverrideError(
            f"'minor_overdue_max_days' must be at least 1 day; got {value}.",
            "days_past_due of 0 is already scored as Current. This threshold "
            "is the boundary between minor and materially overdue.",
        )


def _validate_non_negative_number(value, baseline_rules):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise OverrideError(
            f"Alert threshold must be a plain number; got {value!r}.",
            "Use a number without currency symbols or commas, for example "
            "100000.",
        )

    if value < 0:
        raise OverrideError(
            f"Alert threshold cannot be negative; got {value}.",
            "Use zero or a positive number.",
        )


def _validate_non_negative_int(value, baseline_rules):
    if not isinstance(value, int) or isinstance(value, bool):
        raise OverrideError(
            f"Alert threshold must be a whole number; got {value!r}.",
            "Use an integer, for example 120.",
        )

    if value < 0:
        raise OverrideError(
            f"Alert threshold cannot be negative; got {value}.",
            "Use zero or a positive number.",
        )


ALLOWED_OVERRIDES = {
    "health_model.support_health.average_resolution_hours.bands": {
        "validator": _validate_resolution_bands,
        "reason": (
            "Documented calibration slot. Resolution time is meaningless "
            "without a client benchmark, so the baseline ships it empty."
        ),
    },
    "health_model.support_health.average_resolution_hours.above_points": {
        "validator": _validate_above_points,
        "reason": (
            "Companion to the resolution benchmark above; scores accounts "
            "slower than the slowest configured band."
        ),
    },
    "health_model.commercial_renewal.billing_status.minor_overdue_max_days": {
        "validator": _validate_minor_overdue_days,
        "reason": (
            "Documented calibration slot. The baseline ships it null so "
            "overdue accounts are unavailable rather than scored against an "
            "invented threshold."
        ),
    },
    "alerts.high_risk_min_arr": {
        "validator": _validate_non_negative_number,
        "reason": "Slack alert routing threshold. Does not affect scoring.",
    },
    "alerts.high_risk_max_days_to_renewal": {
        "validator": _validate_non_negative_int,
        "reason": "Slack alert routing threshold. Does not affect scoring.",
    },
    "alerts.known_contraction_min_arr": {
        "validator": _validate_non_negative_number,
        "reason": "Slack alert routing threshold. Does not affect scoring.",
    },
    "alerts.known_expansion_min_arr": {
        "validator": _validate_non_negative_number,
        "reason": "Slack alert routing threshold. Does not affect scoring.",
    },
}


# Paths a client is most likely to reach for that are deliberately closed. The
# generic rejection is correct for all of them; these just produce a message
# that names the reason instead of a bare refusal.
LOCKED_PATH_NOTES = {
    "health_score_bands": "Health Score bands are locked PLS methodology.",
    "retention_risk_by_status": (
        "Retention Risk is derived from Health Status by locked PLS "
        "methodology."
    ),
    "priority_routing": (
        "Executive / CSM routing is locked PLS methodology."
    ),
    "alerts.expansion_alert_requires_low_risk": (
        "Expansion logic is locked. This flag gates expansion alerting and is "
        "not client-configurable."
    ),
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_yaml(path, what):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            f"{what} could not be read: {path}\n  {error}",
            "Check the file exists and is readable.",
        ) from None

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(
            f"{what} is not valid YAML: {path}\n  {error}",
            "Fix the YAML syntax and run again.",
        ) from None

    if loaded is None:
        raise ConfigError(
            f"{what} is empty: {path}",
            "Populate the file. clients/_template has a documented example.",
        )

    if not isinstance(loaded, dict):
        raise ConfigError(
            f"{what} must be a YAML mapping at the top level: {path}",
            "The file should start with keys such as 'client:'.",
        )

    return loaded


def load_baseline_rules(path=None):
    """Load the PLS baseline. Never modified by a client run."""
    baseline_path = Path(path or CANONICAL_BASELINE)

    if not baseline_path.exists():
        raise ConfigError(
            f"Required PLS baseline configuration not found: {baseline_path}",
            "config/health_rules.yaml is mandatory. Restore it from the "
            "repository.",
        )

    rules = _read_yaml(baseline_path, "PLS baseline configuration")

    for required in (
        "health_score_bands",
        "retention_risk_by_status",
        "health_model",
        "priority_routing",
        "alerts",
    ):
        if required not in rules:
            raise ConfigError(
                f"PLS baseline configuration is missing the required "
                f"'{required}' section: {baseline_path}",
                "Restore config/health_rules.yaml from the repository.",
            )

    return rules


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------

def _get_path(mapping, dotted):
    """Resolve a dotted path, returning (found, value)."""
    node = mapping

    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None

        node = node[part]

    return True, node


def _set_path(mapping, dotted, value):
    parts = dotted.split(".")
    node = mapping

    for part in parts[:-1]:
        node = node[part]

    node[parts[-1]] = value


def _locked_note(dotted):
    for prefix, note in LOCKED_PATH_NOTES.items():
        if dotted == prefix or dotted.startswith(prefix + "."):
            return note

    return None


def _validate_slack(raw, client_slug):
    """
    Per-client Slack routing.

    The config may name an environment variable; it may never contain the
    webhook itself. A secret in a client workspace would be committed the first
    time someone forgets, so a value that looks like a URL is a STOP.
    """
    if raw is None:
        return {"enabled": False, "webhook_env_var": None}

    if not isinstance(raw, dict):
        raise ConfigError(
            "'slack' must be a mapping with 'enabled' and 'webhook_env_var'.",
            "See clients/_template/config/client_config.yaml.",
        )

    unknown = set(raw) - SLACK_KEYS
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in 'slack': {', '.join(sorted(unknown))}.",
            f"Supported keys are: {', '.join(sorted(SLACK_KEYS))}.",
        )

    enabled = raw.get("enabled", False)

    if not isinstance(enabled, bool):
        raise ConfigError(
            f"'slack.enabled' must be true or false; got {enabled!r}.",
            "Use an unquoted true or false.",
        )

    env_var = raw.get("webhook_env_var")

    if env_var is not None and not isinstance(env_var, str):
        raise ConfigError(
            f"'slack.webhook_env_var' must be the NAME of an environment "
            f"variable; got {env_var!r}.",
            "For example: webhook_env_var: SLACK_WEBHOOK_ACME",
        )

    if isinstance(env_var, str):
        env_var = env_var.strip()

        if "://" in env_var or env_var.lower().startswith("http"):
            raise ConfigError(
                "'slack.webhook_env_var' looks like a webhook URL. The client "
                "configuration must contain the environment variable NAME "
                "only; the secret stays in .env.",
                "Set webhook_env_var: SLACK_WEBHOOK_"
                f"{client_slug.upper().replace('-', '_')} and put the URL in "
                ".env under that name.",
            )

        if env_var and not env_var.replace("_", "").isalnum():
            raise ConfigError(
                f"'slack.webhook_env_var' is not a valid environment variable "
                f"name: {env_var!r}.",
                "Use letters, digits, and underscores, for example "
                "SLACK_WEBHOOK_ACME.",
            )

    if enabled and not env_var:
        raise ConfigError(
            "'slack.enabled' is true but no 'slack.webhook_env_var' was "
            "given.",
            "Name the environment variable holding this client's webhook, or "
            "set enabled: false.",
        )

    return {"enabled": enabled, "webhook_env_var": env_var or None}


def _validate_overrides(raw, baseline_rules):
    """
    Validate the override block and return normalized entries.

    Four gates, in order: shape, rationale, path exists in the baseline, path
    is on the allowlist. The third gate is what implements "no client override
    may require inventing a new scoring rule".
    """
    if raw is None:
        return []

    if not isinstance(raw, list):
        raise ConfigError(
            "'overrides' must be a list of entries, each with 'path', "
            "'value', and 'rationale'.",
            "See clients/_template/config/client_config.yaml.",
        )

    entries = []
    seen = {}

    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise OverrideError(
                f"Override {index} must be a mapping with 'path', 'value', "
                f"and 'rationale'; got {item!r}.",
                "See clients/_template/config/client_config.yaml.",
            )

        unknown = set(item) - OVERRIDE_KEYS
        if unknown:
            raise OverrideError(
                f"Override {index} has unknown key(s): "
                f"{', '.join(sorted(unknown))}.",
                f"Supported keys are: {', '.join(sorted(OVERRIDE_KEYS))}.",
            )

        dotted = item.get("path")

        if not isinstance(dotted, str) or not dotted.strip():
            raise OverrideError(
                f"Override {index} is missing a 'path'.",
                "Give the dotted configuration path, for example "
                "alerts.high_risk_min_arr.",
            )

        dotted = dotted.strip()

        if "value" not in item:
            raise OverrideError(
                f"Override {index} ({dotted}) is missing a 'value'.",
                "Every override must state the value it applies.",
            )

        rationale = item.get("rationale")

        # D2: an undocumented override is indistinguishable from an accident.
        if not isinstance(rationale, str) or not rationale.strip():
            raise OverrideError(
                f"Override {index} ({dotted}) has no rationale.",
                "Every client override must record why it was applied, who "
                "approved it, and when. This is recorded in the run manifest "
                "and shown to the client.",
            )

        if dotted in seen:
            raise OverrideError(
                f"Override {index} repeats a path already set by override "
                f"{seen[dotted]}: {dotted}.",
                "Remove the duplicate so the applied value is unambiguous.",
            )

        seen[dotted] = index

        exists, baseline_value = _get_path(baseline_rules, dotted)

        if not exists:
            raise OverrideError(
                f"Override {index} names a configuration path that does not "
                f"exist in the PLS baseline: {dotted}",
                "A path that is not already in config/health_rules.yaml would "
                "require inventing a new scoring rule. That is not supported "
                "as a client override. Remove it, or raise a methodology "
                "change request.",
            )

        if dotted not in ALLOWED_OVERRIDES:
            note = _locked_note(dotted)

            raise OverrideError(
                f"Override {index} names a locked configuration path: "
                f"{dotted}"
                + (f"\n  {note}" if note else ""),
                "Client-overridable paths are:\n    - "
                + "\n    - ".join(sorted(ALLOWED_OVERRIDES))
                + "\n  Everything else is locked PLS methodology.",
            )

        ALLOWED_OVERRIDES[dotted]["validator"](item["value"], baseline_rules)

        entries.append(
            {
                "path": dotted,
                "baseline_value": copy.deepcopy(baseline_value),
                "client_value": copy.deepcopy(item["value"]),
                "rationale": rationale.strip(),
                "permitted_because": ALLOWED_OVERRIDES[dotted]["reason"],
            }
        )

    return entries


def load_client_config(workspace, baseline_rules):
    """
    Read and validate a client configuration against the PLS baseline.

    Returns the parsed client identity, Slack routing, and validated override
    entries. Nothing is merged here; see `resolve_health_rules`.
    """
    raw = _read_yaml(workspace.config_path, "Client configuration")

    unknown = set(raw) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(
            f"Unknown top-level key(s) in {workspace.config_path.name}: "
            f"{', '.join(sorted(unknown))}.",
            f"Supported keys are: {', '.join(sorted(TOP_LEVEL_KEYS))}.",
        )

    client = raw.get("client")

    if not isinstance(client, dict):
        raise ConfigError(
            f"'client' section missing from {workspace.config_path}.",
            "Add a client block with 'name' and 'slug'.",
        )

    unknown_client = set(client) - CLIENT_KEYS
    if unknown_client:
        raise ConfigError(
            f"Unknown key(s) in 'client': "
            f"{', '.join(sorted(unknown_client))}.",
            f"Supported keys are: {', '.join(sorted(CLIENT_KEYS))}.",
        )

    name = client.get("name")
    slug = client.get("slug")

    if not isinstance(name, str) or not name.strip():
        raise ConfigError(
            f"'client.name' is required in {workspace.config_path}.",
            "Set the client's display name, for example "
            "name: \"Acme Corporation\".",
        )

    if not isinstance(slug, str) or not slug.strip():
        raise ConfigError(
            f"'client.slug' is required in {workspace.config_path}.",
            f"Set it to the workspace directory name: slug: "
            f"\"{workspace.slug}\".",
        )

    # A slug that disagrees with the directory is the signature of a config
    # copied from another client. That is the exact cross-client mix-up this
    # layer exists to prevent, so it stops the run.
    if slug.strip() != workspace.slug:
        raise ConfigError(
            f"'client.slug' is \"{slug.strip()}\" but the workspace directory "
            f"is \"{workspace.slug}\".",
            "These must match. A mismatch usually means the configuration was "
            "copied from another client. Confirm which client this workspace "
            "belongs to before running.",
        )

    # The baseline key is documented and optional, but it may only ever point
    # at the canonical PLS baseline. Allowing an arbitrary file would let a
    # client replace the whole methodology and bypass the override allowlist.
    declared_baseline = raw.get("baseline")

    if declared_baseline is not None:
        if not isinstance(declared_baseline, str):
            raise ConfigError(
                f"'baseline' must be a path string; got "
                f"{declared_baseline!r}.",
                "Use: baseline: \"config/health_rules.yaml\"",
            )

        candidate = Path(declared_baseline.strip())

        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate

        if candidate.resolve() != CANONICAL_BASELINE.resolve():
            raise ConfigError(
                f"'baseline' must point at the PLS baseline "
                f"({CANONICAL_BASELINE.relative_to(REPO_ROOT)}); got "
                f"{declared_baseline}.",
                "A client cannot substitute a different health model. Use "
                "the overrides block for the supported calibration points.",
            )

    return {
        "name": name.strip(),
        "slug": slug.strip(),
        "engagement": (client.get("engagement") or None),
        "contact": (client.get("contact") or None),
        "slack": _validate_slack(raw.get("slack"), workspace.slug),
        "overrides": _validate_overrides(raw.get("overrides"), baseline_rules),
    }


def resolve_health_rules(baseline_rules, overrides):
    """
    Apply validated overrides to a copy of the baseline.

    The baseline dict itself is never mutated, so a client run cannot leak
    configuration into anything else in the process.
    """
    resolved = copy.deepcopy(baseline_rules)

    for entry in overrides:
        _set_path(resolved, entry["path"], copy.deepcopy(entry["client_value"]))

    return resolved


def dump_resolved_rules(resolved_rules, client, overrides, baseline_path):
    """The exact configuration this run scored with, as reproducible YAML."""
    header = [
        "# =============================================================",
        "# RESOLVED HEALTH CONFIGURATION — generated by CHIA",
        "# =============================================================",
        "# This is the exact configuration used to score this run. It is a",
        "# generated artifact: edit the PLS baseline or the client override",
        "# block instead, never this file.",
        "#",
        f"# Client:   {client['name']} ({client['slug']})",
        f"# Baseline: {baseline_path}",
        f"# Baseline SHA-256: {file_sha256(baseline_path)}",
        "#",
    ]

    if overrides:
        header.append(f"# Client overrides applied: {len(overrides)}")
        for entry in overrides:
            header.append(f"#   - {entry['path']}")
            header.append(f"#       rationale: {entry['rationale']}")
    else:
        header.append(
            "# Client overrides applied: none (pure PLS baseline)"
        )

    header.append(
        "# ============================================================="
    )
    header.append("")

    body = yaml.safe_dump(resolved_rules, sort_keys=False, width=88)

    return "\n".join(header) + body
