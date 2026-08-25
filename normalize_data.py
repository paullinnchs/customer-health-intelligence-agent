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

Client data is validated before processing. Required baseline fields must be
present and dates must be YYYY-MM-DD, so a bad export fails with a message
naming the source, account, and field rather than an unexplained exception.
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

DATE_FORMAT = "%Y-%m-%d"

REQUIRED_ACCOUNT_FIELDS = (
    "account_id",
    "account_name",
    "arr",
    "renewal_date",
)


class DataValidationError(Exception):
    """Raised when client data cannot be processed as supplied."""


def read_csv(filename, required=False):
    path = DATA_DIR / filename

    if not path.exists():
        if required:
            raise DataValidationError(
                f"Required data file not found: {path}\n"
                f"The customer account list is the one mandatory source."
            )
        return []

    with open(path, "r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def describe_row(source, row_number, account_id=None):
    """A consistent way to point a client at the exact row that failed."""
    location = f"{source} (row {row_number})"

    if account_id:
        return f"{location}, account {account_id}"

    return location


def validate_crm_accounts(crm_accounts):
    """
    Validate the required baseline before any processing happens.

    Every problem across the whole file is collected and reported together so a
    client can fix one export in one pass instead of one row per run.
    """
    problems = []
    seen_ids = {}

    if not crm_accounts:
        raise DataValidationError(
            "crm_accounts.csv contains no account rows."
        )

    for index, account in enumerate(crm_accounts, start=2):
        account_id = (account.get("account_id") or "").strip()
        where = describe_row("crm_accounts.csv", index, account_id or None)

        for field in REQUIRED_ACCOUNT_FIELDS:
            if field not in account:
                problems.append(
                    f"{where}: required column '{field}' is missing from the file."
                )
            elif (account.get(field) or "").strip() == "":
                problems.append(
                    f"{where}: required field '{field}' is blank."
                )

        if account_id:
            if account_id in seen_ids:
                problems.append(
                    f"{where}: duplicate account_id, already used on row "
                    f"{seen_ids[account_id]}."
                )
            else:
                seen_ids[account_id] = index

        arr = (account.get("arr") or "").strip()
        if arr:
            try:
                float(arr)
            except ValueError:
                problems.append(
                    f"{where}: field 'arr' must be a plain number without "
                    f"currency symbols or commas, got '{arr}'."
                )

        renewal_date = (account.get("renewal_date") or "").strip()
        if renewal_date:
            problems.extend(
                validate_date(
                    renewal_date,
                    "renewal_date",
                    "crm_accounts.csv",
                    index,
                    account_id,
                )
            )

        contract_start = (account.get("contract_start") or "").strip()
        if contract_start:
            problems.extend(
                validate_date(
                    contract_start,
                    "contract_start",
                    "crm_accounts.csv",
                    index,
                    account_id,
                )
            )

    if problems:
        raise DataValidationError(
            "Customer account data could not be processed:\n  - "
            + "\n  - ".join(problems)
        )


def validate_date(value, field, source, row_number, account_id=None):
    """Return a list of problems (empty when the date is valid)."""
    try:
        datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        where = describe_row(source, row_number, account_id)
        return [
            f"{where}: field '{field}' must use the YYYY-MM-DD date format, "
            f"got '{value}'."
        ]

    return []


def validate_optional_dates(rows, source, fields):
    """
    Validate dates in the optional sources.

    These are not required, but a malformed value would otherwise surface as a
    raw strptime traceback with no indication of which file or row caused it.
    """
    problems = []

    for index, row in enumerate(rows, start=2):
        account_id = (row.get("account_id") or "").strip()

        for field in fields:
            value = (row.get(field) or "").strip()

            if value:
                problems.extend(
                    validate_date(
                        value, field, source, index, account_id or None
                    )
                )

    return problems


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

    target_date = datetime.strptime(date_string, DATE_FORMAT)
    today = datetime.today()

    return (target_date - today).days


def days_since(date_string):
    if not date_string:
        return None

    event_date = datetime.strptime(date_string, DATE_FORMAT)
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

    validate_crm_accounts(crm_accounts)

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

    date_problems = []
    date_problems.extend(
        validate_optional_dates(
            product_usage, "product_usage.csv", ["last_active_date"]
        )
    )
    date_problems.extend(
        validate_optional_dates(
            support_tickets, "support_tickets.csv", ["created_date"]
        )
    )
    date_problems.extend(
        validate_optional_dates(
            customer_engagement,
            "customer_engagement.csv",
            ["last_csm_meeting", "last_exec_meeting"],
        )
    )
    date_problems.extend(
        validate_optional_dates(
            customer_sentiment, "customer_sentiment.csv", ["survey_date"]
        )
    )

    if date_problems:
        raise DataValidationError(
            "Customer health signal data could not be processed:\n  - "
            + "\n  - ".join(date_problems)
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

            # Blank commercial cells stay None rather than collapsing to 0.
            # A blank is "not available", which the scoring model treats as an
            # unavailable signal. Only an explicit 0 means "none known".
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
                    float_or_none(
                        billing_record.get(
                            "contract_value"
                        )
                    ),
                "expansion_arr":
                    float_or_none(
                        billing_record.get(
                            "expansion_arr"
                        )
                    ),
                "contraction_arr":
                    float_or_none(
                        billing_record.get(
                            "contraction_arr"
                        )
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
