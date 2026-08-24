"""
normalize_data.py
-----------------
Normalizes customer-health signals from multiple CSV sources into one
account-level record for analysis.

Required:
- sample_data/crm_accounts.csv

Optional:
- sample_data/product_usage.csv
- sample_data/support_tickets.csv
- sample_data/customer_engagement.csv
- sample_data/customer_sentiment.csv
- sample_data/billing.csv

Missing optional sources are represented explicitly in data_availability so
the analysis layer does not confuse missing data with a healthy zero value.
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.getenv(
        "CUSTOMER_HEALTH_DATA_DIR",
        str(BASE_DIR / "sample_data"),
    )
)
OUTPUT_DIR = BASE_DIR / "outputs"


def read_csv(filename, required=False):
    path = DATA_DIR / filename

    if not path.exists():
        if required:
            raise FileNotFoundError(
                f"Required data file not found: {path}"
            )
        return []

    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def int_or_none(value):
    if value in (None, ""):
        return None
    return int(float(value))


def float_or_none(value):
    if value in (None, ""):
        return None
    return float(value)


def days_until(date_string):
    if not date_string:
        return None

    target_date = datetime.strptime(date_string, "%Y-%m-%d")
    today = datetime.today()

    return (target_date - today).days


def days_since(date_string):
    if not date_string:
        return None

    event_date = datetime.strptime(date_string, "%Y-%m-%d")
    today = datetime.today()

    return (today - event_date).days


def average(values):
    clean = [value for value in values if value is not None]

    if not clean:
        return None

    return round(sum(clean) / len(clean), 1)


def normalize_accounts():
    crm_accounts = read_csv(
        "crm_accounts.csv",
        required=True,
    )

    product_usage = read_csv(
        "product_usage.csv",
    )

    support_tickets = read_csv(
        "support_tickets.csv",
    )

    customer_engagement = read_csv(
        "customer_engagement.csv",
    )

    customer_sentiment = read_csv(
        "customer_sentiment.csv",
    )

    billing = read_csv(
        "billing.csv",
    )

    source_presence = {
        "crm": bool(crm_accounts),
        "product_usage": bool(product_usage),
        "support": bool(support_tickets),
        "engagement": bool(customer_engagement),
        "sentiment": bool(customer_sentiment),
        "billing": bool(billing),
    }

    usage_by_account = {
        row["account_id"]: row
        for row in product_usage
    }

    engagement_by_account = {
        row["account_id"]: row
        for row in customer_engagement
    }

    sentiment_by_account = {
        row["account_id"]: row
        for row in customer_sentiment
    }

    billing_by_account = {
        row["account_id"]: row
        for row in billing
    }

    tickets_by_account = defaultdict(list)

    for ticket in support_tickets:
        tickets_by_account[
            ticket["account_id"]
        ].append(ticket)

    normalized_accounts = []

    for account in crm_accounts:
        account_id = account["account_id"]

        usage = usage_by_account.get(
            account_id
        )

        engagement = engagement_by_account.get(
            account_id
        )

        sentiment = sentiment_by_account.get(
            account_id
        )

        billing_record = billing_by_account.get(
            account_id
        )

        tickets = tickets_by_account.get(
            account_id,
            [],
        )

        account_source_presence = {
            "crm": True,
            "product_usage": usage is not None,
            "support": (
                source_presence["support"]
                and any(
                    ticket.get("account_id")
                    == account_id
                    for ticket in support_tickets
                )
            ),
            "engagement": engagement is not None,
            "sentiment": sentiment is not None,
            "billing": billing_record is not None,
        }

        usage = usage or {}
        engagement = engagement or {}
        sentiment = sentiment or {}
        billing_record = billing_record or {}

        high_severity_tickets = sum(
            1
            for ticket in tickets
            if ticket.get(
                "priority",
                "",
            ).lower()
            in {"high", "critical"}
        )

        open_tickets = sum(
            1
            for ticket in tickets
            if ticket.get(
                "status",
                "",
            ).lower()
            != "closed"
        )

        resolution_hours = [
            float_or_none(
                ticket.get(
                    "resolution_hours"
                )
            )
            for ticket in tickets
        ]

        csat_scores = [
            float_or_none(
                ticket.get("csat")
            )
            for ticket in tickets
        ]

        current_nps = int_or_none(
            sentiment.get("nps")
        )

        previous_nps = int_or_none(
            sentiment.get(
                "previous_nps"
            )
        )

        licensed_users = int_or_none(
            usage.get(
                "licensed_users"
            )
        )

        monthly_active_users = int_or_none(
            usage.get(
                "monthly_active_users"
            )
        )

        if (
            licensed_users
            and monthly_active_users
            is not None
        ):
            utilization = round(
                monthly_active_users
                / licensed_users
                * 100,
                1,
            )
        else:
            utilization = None

        if (
            current_nps is not None
            and previous_nps is not None
        ):
            nps_change = (
                current_nps
                - previous_nps
            )
        else:
            nps_change = None

        qbr_raw = engagement.get(
            "qbr_completed"
        )

        if qbr_raw in (None, ""):
            qbr_completed = None
        else:
            qbr_completed = (
                str(qbr_raw).lower()
                == "true"
            )

        normalized_record = {
            "account_id": account_id,
            "account_name": account[
                "account_name"
            ],
            "segment": account.get(
                "segment"
            ),
            "arr": float(
                account["arr"]
            ),
            "contract_start": account.get(
                "contract_start"
            ),
            "renewal_date": account.get(
                "renewal_date"
            ),
            "days_to_renewal": days_until(
                account.get(
                    "renewal_date"
                )
            ),
            "csm": account.get(
                "csm"
            ),
            "industry": account.get(
                "industry"
            ),
            "employee_count": int_or_none(
                account.get(
                    "employee_count"
                )
            ),
            "account_stage": account.get(
                "account_stage"
            ),

            "data_availability":
                account_source_presence,

            "product_usage": {
                "monthly_active_users":
                    monthly_active_users,
                "licensed_users":
                    licensed_users,
                "license_utilization_pct":
                    utilization,
                "login_change_60d_pct":
                    float_or_none(
                        usage.get(
                            "login_change_60d_pct"
                        )
                    ),
                "core_feature_adoption_pct":
                    float_or_none(
                        usage.get(
                            "core_feature_adoption_pct"
                        )
                    ),
                "automation_adoption_pct":
                    float_or_none(
                        usage.get(
                            "automation_adoption_pct"
                        )
                    ),
                "usage_trend":
                    usage.get(
                        "usage_trend"
                    ),
                "last_active_date":
                    usage.get(
                        "last_active_date"
                    ),
            },

            "support": {
                "ticket_count": (
                    len(tickets)
                    if account_source_presence[
                        "support"
                    ]
                    else None
                ),
                "high_severity_tickets": (
                    high_severity_tickets
                    if account_source_presence[
                        "support"
                    ]
                    else None
                ),
                "open_tickets": (
                    open_tickets
                    if account_source_presence[
                        "support"
                    ]
                    else None
                ),
                "average_resolution_hours":
                    average(
                        resolution_hours
                    ),
                "average_csat":
                    average(
                        csat_scores
                    ),
            },

            "engagement": {
                "last_csm_meeting":
                    engagement.get(
                        "last_csm_meeting"
                    ),
                "days_since_last_csm_meeting":
                    days_since(
                        engagement.get(
                            "last_csm_meeting"
                        )
                    ),
                "last_exec_meeting":
                    engagement.get(
                        "last_exec_meeting"
                    ),
                "days_since_last_exec_meeting":
                    days_since(
                        engagement.get(
                            "last_exec_meeting"
                        )
                    ),
                "qbr_completed":
                    qbr_completed,
                "champion_status":
                    engagement.get(
                        "champion_status"
                    ),
                "stakeholder_coverage":
                    engagement.get(
                        "stakeholder_coverage"
                    ),
                "open_action_items":
                    int_or_none(
                        engagement.get(
                            "open_action_items"
                        )
                    ),
            },

            "sentiment": {
                "current_nps":
                    current_nps,
                "previous_nps":
                    previous_nps,
                "nps_change":
                    nps_change,
                "sentiment":
                    sentiment.get(
                        "sentiment"
                    ),
                "primary_feedback":
                    sentiment.get(
                        "primary_feedback"
                    ),
            },

            "billing": {
                "billing_status":
                    billing_record.get(
                        "billing_status"
                    ),
                "days_past_due":
                    int_or_none(
                        billing_record.get(
                            "days_past_due"
                        )
                    ),
                "contract_value":
                    float(
                        billing_record.get(
                            "contract_value",
                            0,
                        )
                        or 0
                    ),
                "expansion_arr":
                    float(
                        billing_record.get(
                            "expansion_arr",
                            0,
                        )
                        or 0
                    ),
                "contraction_arr":
                    float(
                        billing_record.get(
                            "contraction_arr",
                            0,
                        )
                        or 0
                    ),
            },
        }

        normalized_accounts.append(
            normalized_record
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        OUTPUT_DIR
        / "normalized_accounts.json"
    )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            normalized_accounts,
            file,
            indent=2,
        )

    return normalized_accounts


if __name__ == "__main__":
    accounts = normalize_accounts()

    print(
        f"Normalized "
        f"{len(accounts)} "
        f"customer accounts."
    )

    print(
        "Output written to: "
        f"{OUTPUT_DIR / 'normalized_accounts.json'}"
    )
