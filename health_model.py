"""
health_model.py
---------------
The PLS Baseline Health Model.

This module owns every number in the customer-health assessment. It is fully
deterministic: the same normalized account record and the same configuration
always produce the same Health Score, Health Status, Retention Risk, Signal
Coverage, and component breakdown.

The LLM is not involved here. It receives these results as context and returns
narrative only.

Missing signals are never scored as zero. Each subcomponent reports whether it
was available, and the score is normalized against the points that were
actually available:

    Health Score   = points_earned / points_available * 100
    Signal Coverage = points_available / points_configured * 100

All weights, bands, and thresholds live in config/health_rules.yaml.
"""

import math


COMPONENT_ORDER = [
    "product_adoption",
    "customer_engagement",
    "customer_sentiment",
    "support_health",
    "commercial_renewal",
]


class HealthModelConfigError(Exception):
    """Raised when the configuration cannot support a deterministic score."""


def _round_half_up(value):
    """
    Round half away from zero.

    Python's built-in round() uses banker's rounding, which would turn 76.5
    into 76 and 77.5 into 78. Client-facing scores should round predictably.
    """
    return int(math.floor(value + 0.5))


def _subcomponent(label, earned, possible, available):
    return {
        "label": label,
        "earned": earned if available else 0,
        "possible": possible,
        "available": available,
    }


def _score_at_least(value, rules):
    """Higher values are better. First band the value meets or exceeds wins."""
    bands = rules.get("bands") or []
    possible = _declared_max(rules, bands, "points")

    if value is None:
        return _subcomponent(rules["label"], 0, possible, False)

    for band in bands:
        if value >= band["at_least"]:
            return _subcomponent(rules["label"], band["points"], possible, True)

    return _subcomponent(
        rules["label"], rules.get("below_points", 0), possible, True
    )


def _score_at_most(value, rules):
    """Lower values are better. First band the value is at or under wins."""
    bands = rules.get("bands") or []
    possible = _declared_max(rules, bands, "points")

    if value is None:
        return _subcomponent(rules["label"], 0, possible, False)

    for band in bands:
        if value <= band["at_most"]:
            return _subcomponent(rules["label"], band["points"], possible, True)

    return _subcomponent(
        rules["label"], rules.get("above_points", 0), possible, True
    )


def _declared_max(rules, bands, key):
    """
    A subcomponent's possible points.

    Normally this is the highest band value, but an explicit `max_points` wins.
    That matters when a subcomponent has no bands configured yet (Resolution
    Performance) and still needs to count toward configured points so that
    Signal Coverage reflects the gap honestly.
    """
    if "max_points" in rules and rules["max_points"] is not None:
        return rules["max_points"]

    if not bands:
        raise HealthModelConfigError(
            f"Subcomponent '{rules.get('label', '?')}' has no bands and no "
            f"max_points, so its possible points cannot be determined."
        )

    return max(band[key] for band in bands)


def _score_bands_or_unavailable(value, rules, mode):
    """
    Score a banded subcomponent, treating an empty band list as unavailable.

    This is how Resolution Performance behaves until a client benchmark is
    calibrated: the points stay in the configured total but are never available.
    """
    if not rules.get("bands"):
        return _subcomponent(
            rules["label"], 0, _declared_max(rules, [], "points"), False
        )

    if mode == "at_least":
        return _score_at_least(value, rules)

    return _score_at_most(value, rules)


def _score_category(value, rules):
    """
    Map a categorical value to points.

    An unrecognized or blank value makes the subcomponent unavailable. It is not
    scored as zero, because an unknown champion status is not the same thing as
    a lost champion.
    """
    possible = rules["max_points"]

    if value is None or str(value).strip() == "":
        return _subcomponent(rules["label"], 0, possible, False)

    key = str(value).strip().lower()
    values = rules["values"]

    if key not in values:
        return _subcomponent(rules["label"], 0, possible, False)

    return _subcomponent(rules["label"], values[key], possible, True)


def _score_boolean(value, rules):
    possible = max(rules["true_points"], rules["false_points"])

    if value is None:
        return _subcomponent(rules["label"], 0, possible, False)

    points = rules["true_points"] if value else rules["false_points"]
    return _subcomponent(rules["label"], points, possible, True)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def _score_product_adoption(account, rules):
    usage = account["product_usage"]

    return [
        _score_at_least(
            usage["license_utilization_pct"], rules["license_utilization_pct"]
        ),
        _score_at_least(
            usage["login_change_60d_pct"], rules["login_change_60d_pct"]
        ),
        _score_at_least(
            usage["core_feature_adoption_pct"],
            rules["core_feature_adoption_pct"],
        ),
        _score_at_least(
            usage["automation_adoption_pct"], rules["automation_adoption_pct"]
        ),
    ]


def _score_customer_engagement(account, rules):
    engagement = account["engagement"]

    return [
        _score_at_most(
            engagement["days_since_last_csm_meeting"],
            rules["days_since_last_csm_meeting"],
        ),
        _score_at_most(
            engagement["days_since_last_exec_meeting"],
            rules["days_since_last_exec_meeting"],
        ),
        _score_boolean(engagement["qbr_completed"], rules["qbr_completed"]),
        _score_category(
            engagement["champion_status"], rules["champion_status"]
        ),
        _score_category(
            engagement["stakeholder_coverage"], rules["stakeholder_coverage"]
        ),
        _score_at_most(
            engagement["open_action_items"], rules["open_action_items"]
        ),
    ]


def _score_customer_sentiment(account, rules):
    sentiment = account["sentiment"]

    return [
        _score_at_least(sentiment["current_nps"], rules["current_nps"]),
        _score_at_least(sentiment["nps_change"], rules["nps_change"]),
        _score_category(sentiment["sentiment"], rules["sentiment"]),
    ]


def _score_support_health(account, rules):
    support = account["support"]

    return [
        _score_at_most(
            support["high_severity_tickets"], rules["high_severity_tickets"]
        ),
        _score_at_most(support["open_tickets"], rules["open_tickets"]),
        _score_at_least(support["average_csat"], rules["average_csat"]),
        _score_bands_or_unavailable(
            support["average_resolution_hours"],
            rules["average_resolution_hours"],
            mode="at_most",
        ),
    ]


def _score_known_contraction(account, rules):
    """
    Known Contraction is a client-supplied commercial fact.

    Unavailable when the account has no billing record or the contraction cell
    is blank, so an absent billing source cannot earn "no contraction" points.
    """
    possible = max(rules["none_points"], rules["any_points"])
    billing_available = account["data_availability"]["billing"]
    contraction = account["billing"]["contraction_arr"]

    if not billing_available or contraction is None:
        return _subcomponent(rules["label"], 0, possible, False)

    points = (
        rules["any_points"] if contraction > 0 else rules["none_points"]
    )
    return _subcomponent(rules["label"], points, possible, True)


def _score_billing_status(account, rules):
    """
    Billing Status is scored from days_past_due, not the free-text column.

    Accounts that are overdue cannot be scored until `minor_overdue_max_days`
    is calibrated for the client, so they are reported as unavailable rather
    than measured against an invented threshold.
    """
    possible = rules["max_points"]
    billing_available = account["data_availability"]["billing"]
    days_past_due = account["billing"]["days_past_due"]

    if not billing_available or days_past_due is None:
        return _subcomponent(rules["label"], 0, possible, False)

    if days_past_due <= 0:
        return _subcomponent(
            rules["label"], rules["current_points"], possible, True
        )

    cutoff = rules.get("minor_overdue_max_days")

    if cutoff is None:
        return _subcomponent(rules["label"], 0, possible, False)

    points = (
        rules["minor_overdue_points"]
        if days_past_due <= cutoff
        else rules["materially_overdue_points"]
    )
    return _subcomponent(rules["label"], points, possible, True)


def _score_commercial_renewal(account, rules):
    return [
        _score_known_contraction(account, rules["known_contraction"]),
        _score_billing_status(account, rules["billing_status"]),
        _score_at_least(
            account["days_to_renewal"], rules["renewal_proximity"]
        ),
    ]


COMPONENT_SCORERS = {
    "product_adoption": _score_product_adoption,
    "customer_engagement": _score_customer_engagement,
    "customer_sentiment": _score_customer_sentiment,
    "support_health": _score_support_health,
    "commercial_renewal": _score_commercial_renewal,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_account(account, health_rules):
    """
    Score one normalized account record.

    Returns a dict containing the composite Health Score, Health Status,
    Retention Risk, Signal Coverage, and the per-component breakdown.
    """
    model_rules = health_rules["health_model"]
    components = []

    for name in COMPONENT_ORDER:
        rules = model_rules[name]
        subcomponents = COMPONENT_SCORERS[name](account, rules)

        earned = sum(
            item["earned"] for item in subcomponents if item["available"]
        )
        available = sum(
            item["possible"] for item in subcomponents if item["available"]
        )
        configured = sum(item["possible"] for item in subcomponents)

        components.append(
            {
                "key": name,
                "label": rules["label"],
                "earned": earned,
                "available": available,
                "configured": configured,
                "subcomponents": subcomponents,
            }
        )

    points_earned = sum(item["earned"] for item in components)
    points_available = sum(item["available"] for item in components)
    points_configured = sum(item["configured"] for item in components)

    if points_available == 0:
        raise HealthModelConfigError(
            f"Account {account['account_id']} has no available health signals, "
            f"so a Health Score cannot be calculated."
        )

    health_score = _round_half_up(points_earned / points_available * 100)
    health_score = max(0, min(100, health_score))

    health_status = derive_health_status(health_score, health_rules)
    retention_risk = derive_retention_risk(health_status, health_rules)

    signal_coverage = _round_half_up(
        points_available / points_configured * 100
    )

    return {
        "health_score": health_score,
        "health_status": health_status,
        "retention_risk": retention_risk,
        "points_earned": points_earned,
        "points_available": points_available,
        "points_configured": points_configured,
        "signal_coverage_pct": signal_coverage,
        "components": components,
        "unavailable_signals": [
            f"{component['label']} — {item['label']}"
            for component in components
            for item in component["subcomponents"]
            if not item["available"]
        ],
    }


def derive_health_status(score, health_rules):
    bands = health_rules["health_score_bands"]

    if score >= bands["healthy_min"]:
        return "Healthy"

    if score >= bands["monitor_min"]:
        return "Monitor"

    if score >= bands["at_risk_min"]:
        return "At Risk"

    return "Critical"


def derive_retention_risk(health_status, health_rules):
    mapping = health_rules["retention_risk_by_status"]

    if health_status not in mapping:
        raise HealthModelConfigError(
            f"No Retention Risk configured for Health Status "
            f"'{health_status}'. Check retention_risk_by_status in "
            f"config/health_rules.yaml."
        )

    return mapping[health_status]
