from __future__ import annotations

import argparse
import json
import os
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


ISSUE_TYPES = [
    "delayed_delivery",
    "damaged_item",
    "product_quality_complaint",
    "missing_item",
    "payment_question",
    "negative_review_escalation",
]

LOGISTICS_INCIDENT_TYPES = [
    "carrier_delay",
    "regional_delivery_bottleneck",
    "warehouse_handoff_issue",
    "seller_dispatch_delay",
    "address_routing_issue",
]

SUPPORT_CHANNELS = [
    "email",
    "web_portal",
    "chat",
    "phone",
]

TICKET_STATUSES = [
    "open",
    "in_progress",
    "waiting_for_seller",
    "waiting_for_customer",
    "resolved",
    "escalated",
]

POLICY_TOPICS = [
    "late_delivery_refund_policy",
    "damaged_item_return_policy",
    "seller_escalation_policy",
    "negative_review_response_policy",
    "payment_investigation_policy",
    "warranty_claim_policy",
]


def build_postgres_engine() -> Engine:
    load_dotenv()

    db_name = os.getenv("POSTGRES_DB", "enterprise_ai")
    db_user = os.getenv("POSTGRES_USER", "enterprise_user")
    db_password = os.getenv("POSTGRES_PASSWORD", "enterprise_password")
    db_port = os.getenv("POSTGRES_PORT", "5432")
    db_host = os.getenv("POSTGRES_HOST", "localhost")

    connection_url = (
        f"postgresql+psycopg2://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
    )

    return create_engine(connection_url)


def sanitize_value(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, float) and pd.isna(value):
        return None

    if pd.isna(value):
        return None

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()

    if isinstance(value, np.generic):
        return value.item()

    return value


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: sanitize_value(value)
        for key, value in record.items()
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_dataframe(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    return pd.read_sql(text(sql), engine, params=params)


def generate_id(prefix: str, index: int) -> str:
    return f"{prefix}-{index:06d}"


def choose_created_at(row: pd.Series, offset_days_min: int = 1, offset_days_max: int = 12) -> str:
    base_value = row.get("order_delivered_customer_date")

    if pd.isna(base_value) or base_value is None:
        base_value = row.get("order_purchase_timestamp")

    if pd.isna(base_value) or base_value is None:
        base_dt = datetime.now(timezone.utc)
    else:
        base_dt = pd.to_datetime(base_value).to_pydatetime()

    created_at = base_dt + timedelta(
        days=random.randint(offset_days_min, offset_days_max),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )

    return created_at.isoformat()


def normalize_text(value: Any, fallback: str = "") -> str:
    if value is None or pd.isna(value):
        return fallback

    text_value = str(value).strip()

    if text_value.lower() in {"", "nan", "none"}:
        return fallback

    return text_value


def determine_issue_type(row: pd.Series) -> str:
    review_score = row.get("review_score")
    is_late_delivery = row.get("is_late_delivery")
    delivery_delay_days = row.get("delivery_delay_days")

    if pd.notna(review_score):
        score = int(review_score)

        if score <= 1:
            return random.choice(
                [
                    "damaged_item",
                    "missing_item",
                    "product_quality_complaint",
                    "negative_review_escalation",
                ]
            )

        if score == 2:
            return random.choice(
                [
                    "product_quality_complaint",
                    "damaged_item",
                    "negative_review_escalation",
                ]
            )

    if is_late_delivery is True or str(is_late_delivery).lower() == "true":
        return "delayed_delivery"

    if pd.notna(delivery_delay_days):
        try:
            if float(delivery_delay_days) > 3:
                return "delayed_delivery"
        except ValueError:
            pass

    payment_types = normalize_text(row.get("payment_types"))

    if "voucher" in payment_types.lower() or "boleto" in payment_types.lower():
        return "payment_question"

    return random.choice(ISSUE_TYPES)


def determine_severity(issue_type: str, row: pd.Series) -> str:
    review_score = row.get("review_score")
    delivery_delay_days = row.get("delivery_delay_days")

    if issue_type == "delayed_delivery" and pd.notna(delivery_delay_days):
        try:
            delay_days = float(delivery_delay_days)

            if delay_days >= 10:
                return "critical"

            if delay_days >= 5:
                return "high"
        except ValueError:
            pass

    if pd.notna(review_score):
        score = int(review_score)

        if score <= 1:
            return "high"

        if score == 2:
            return "medium"

    if issue_type in {"damaged_item", "missing_item", "negative_review_escalation"}:
        return random.choice(["medium", "high"])

    return random.choice(["low", "medium"])


def ticket_title(issue_type: str, category: str | None) -> str:
    category_text = category or "purchased product"

    titles = {
        "delayed_delivery": f"Customer reported delayed delivery for {category_text}",
        "damaged_item": f"Customer reported damaged {category_text}",
        "product_quality_complaint": f"Product quality complaint for {category_text}",
        "missing_item": f"Customer reported missing item for {category_text}",
        "payment_question": "Customer requested payment clarification",
        "negative_review_escalation": f"Negative review escalation for {category_text}",
    }

    return titles.get(issue_type, f"Support case for {category_text}")


def ticket_customer_message(issue_type: str, row: pd.Series) -> str:
    category = normalize_text(
        row.get("product_category_name_english"),
        normalize_text(row.get("product_category_name"), "the product"),
    )

    delay_days = row.get("delivery_delay_days")
    review_comment = normalize_text(row.get("review_comment_message"))

    if review_comment and "semantic_placeholder" not in review_comment.lower():
        return review_comment

    if issue_type == "delayed_delivery":
        return (
            f"I am contacting support because my order arrived later than expected. "
            f"The product category was {category}. "
            f"The delay affected my experience and I would like an explanation."
        )

    if issue_type == "damaged_item":
        return (
            f"The item from category {category} arrived damaged. "
            f"I need help with replacement, refund, or seller escalation."
        )

    if issue_type == "product_quality_complaint":
        return (
            f"The quality of the {category} product did not match my expectations. "
            f"I would like the support team to review this case."
        )

    if issue_type == "missing_item":
        return (
            f"My order was delivered, but at least one expected item appears to be missing. "
            f"Please check the seller and order item details."
        )

    if issue_type == "payment_question":
        return (
            f"I need clarification about the payment records for this order. "
            f"Please confirm the payment method and charged amount."
        )

    if pd.notna(delay_days):
        return (
            f"I had a poor experience with this order. "
            f"The product category was {category}, and I would like support to investigate."
        )

    return "I had a poor experience with this order and need support assistance."


def build_linked_entities(row: pd.Series) -> dict[str, Any]:
    return sanitize_record(
        {
            "customer_id": row.get("customer_id"),
            "customer_unique_id": row.get("customer_unique_id"),
            "order_id": row.get("order_id"),
            "product_id": row.get("product_id"),
            "seller_id": row.get("seller_id"),
            "review_id": row.get("review_id"),
            "category_id": row.get("product_category_name"),
            "category_name_english": row.get("product_category_name_english"),
            "customer_state": row.get("customer_state"),
            "customer_city": row.get("customer_city"),
            "region_id": (
                f"{row.get('customer_state')}::{row.get('customer_city')}"
                if pd.notna(row.get("customer_state")) and pd.notna(row.get("customer_city"))
                else None
            ),
        }
    )


def build_ticket_document_text(record: dict[str, Any]) -> str:
    linked = record["linked_entities"]

    return (
        f"Support Ticket {record['ticket_id']} concerns order {linked.get('order_id')} "
        f"from customer {linked.get('customer_id')}. "
        f"The issue type is {record['issue_type']} with severity {record['severity']}. "
        f"The related product is {linked.get('product_id')} in category "
        f"{linked.get('category_name_english') or linked.get('category_id')}. "
        f"The related seller is {linked.get('seller_id')}. "
        f"Customer message: {record['customer_message']} "
        f"Agent notes: {record['agent_notes']}"
    )


def fetch_support_candidate_orders(engine: Engine, max_records: int) -> pd.DataFrame:
    sql = """
    WITH first_item AS (
        SELECT DISTINCT ON (oi.order_id)
            oi.order_id,
            oi.product_id,
            oi.seller_id,
            p.product_category_name,
            pct.product_category_name_english
        FROM ecommerce.order_items oi
        JOIN ecommerce.products p
            ON oi.product_id = p.product_id
        LEFT JOIN ecommerce.product_category_translations pct
            ON p.product_category_name = pct.product_category_name
        WHERE
            oi.product_id IS NOT NULL
            AND oi.seller_id IS NOT NULL
            AND p.product_category_name IS NOT NULL
        ORDER BY oi.order_id, oi.price DESC NULLS LAST
    ),

    lowest_review AS (
        SELECT DISTINCT ON (r.order_id)
            r.order_id,
            r.review_id,
            r.review_score,
            r.sentiment_label,
            r.review_comment_message
        FROM ecommerce.reviews r
        ORDER BY r.order_id, r.review_score ASC NULLS LAST, r.review_creation_date DESC NULLS LAST
    ),

    candidate_orders AS (
        SELECT
            os.order_id,
            os.customer_id,
            os.customer_unique_id,
            os.customer_city,
            os.customer_state,
            os.order_status,
            os.delivery_status,
            os.order_purchase_timestamp,
            os.order_delivered_customer_date,
            os.order_estimated_delivery_date,
            os.is_late_delivery,
            os.delivery_delay_days,
            os.total_payment_value,
            os.payment_types,
            os.avg_review_score,
            os.min_review_score,
            fi.product_id,
            fi.seller_id,
            fi.product_category_name,
            fi.product_category_name_english,
            lr.review_id,
            lr.review_score,
            lr.sentiment_label,
            lr.review_comment_message,

            CASE
                WHEN lr.review_score <= 2 THEN 4
                WHEN os.is_late_delivery = TRUE THEN 3
                WHEN os.payment_record_count > 1 THEN 2
                WHEN os.review_count > 0 THEN 1
                ELSE 0
            END AS candidate_priority

        FROM ecommerce.vw_order_summary os
        JOIN first_item fi
            ON os.order_id = fi.order_id
        LEFT JOIN lowest_review lr
            ON os.order_id = lr.order_id
        WHERE
            lr.review_score <= 2
            OR os.is_late_delivery = TRUE
            OR os.payment_record_count > 1
            OR os.review_count > 0
    )

    SELECT *
    FROM candidate_orders
    ORDER BY
        candidate_priority DESC,
        min_review_score ASC NULLS LAST,
        delivery_delay_days DESC NULLS LAST,
        total_payment_value DESC NULLS LAST
    LIMIT :max_records;
    """

    return read_dataframe(engine, sql, {"max_records": max_records})

def generate_support_tickets(candidate_orders: pd.DataFrame) -> list[dict[str, Any]]:
    records = []

    for index, (_, row) in enumerate(candidate_orders.iterrows(), start=1):
        issue_type = determine_issue_type(row)
        severity = determine_severity(issue_type, row)
        category = normalize_text(
            row.get("product_category_name_english"),
            normalize_text(row.get("product_category_name"), "unknown category"),
        )

        record = {
            "artifact_type": "support_ticket",
            "ticket_id": generate_id("TCK", index),
            "created_at": choose_created_at(row),
            "status": random.choice(TICKET_STATUSES),
            "channel": random.choice(SUPPORT_CHANNELS),
            "issue_type": issue_type,
            "severity": severity,
            "title": ticket_title(issue_type, category),
            "summary": (
                f"Customer support case for order {row.get('order_id')} involving "
                f"{issue_type.replace('_', ' ')}."
            ),
            "customer_message": ticket_customer_message(issue_type, row),
            "agent_notes": (
                f"Investigate order lifecycle, seller responsibility, payment context, "
                f"review score, and category-specific handling policy."
            ),
            "linked_entities": build_linked_entities(row),
            "metadata": sanitize_record(
                {
                    "order_status": row.get("order_status"),
                    "delivery_status": row.get("delivery_status"),
                    "is_late_delivery": row.get("is_late_delivery"),
                    "delivery_delay_days": row.get("delivery_delay_days"),
                    "review_score": row.get("review_score"),
                    "sentiment_label": row.get("sentiment_label"),
                    "total_payment_value": row.get("total_payment_value"),
                    "source": "synthetic_template_generator",
                }
            ),
        }

        record["document_text"] = build_ticket_document_text(record)
        records.append(sanitize_record(record))

    return records


def fetch_logistics_candidates(engine: Engine, max_records: int) -> pd.DataFrame:
    sql = """
    WITH first_item AS (
        SELECT DISTINCT ON (oi.order_id)
            oi.order_id,
            oi.product_id,
            oi.seller_id,
            p.product_category_name,
            pct.product_category_name_english
        FROM ecommerce.order_items oi
        JOIN ecommerce.products p
            ON oi.product_id = p.product_id
        LEFT JOIN ecommerce.product_category_translations pct
            ON p.product_category_name = pct.product_category_name
        WHERE
            oi.product_id IS NOT NULL
            AND oi.seller_id IS NOT NULL
            AND p.product_category_name IS NOT NULL
        ORDER BY oi.order_id, oi.price DESC NULLS LAST
    )

    SELECT
        os.order_id,
        os.customer_id,
        os.customer_unique_id,
        os.customer_city,
        os.customer_state,
        os.order_status,
        os.delivery_status,
        os.order_purchase_timestamp,
        os.order_delivered_customer_date,
        os.order_estimated_delivery_date,
        os.is_late_delivery,
        os.delivery_delay_days,
        os.total_payment_value,
        fi.product_id,
        fi.seller_id,
        fi.product_category_name,
        fi.product_category_name_english
    FROM ecommerce.vw_order_summary os
    JOIN first_item fi
        ON os.order_id = fi.order_id
    WHERE os.is_late_delivery = TRUE
    ORDER BY os.delivery_delay_days DESC NULLS LAST
    LIMIT :max_records;
    """

    return read_dataframe(engine, sql, {"max_records": max_records})


def generate_logistics_incidents(candidate_orders: pd.DataFrame) -> list[dict[str, Any]]:
    records = []

    for index, (_, row) in enumerate(candidate_orders.iterrows(), start=1):
        incident_type = random.choice(LOGISTICS_INCIDENT_TYPES)

        delay_days = row.get("delivery_delay_days")

        if pd.notna(delay_days):
            delay_value = float(delay_days)
        else:
            delay_value = 0.0

        if delay_value >= 10:
            severity = "critical"
        elif delay_value >= 5:
            severity = "high"
        else:
            severity = "medium"

        linked_entities = build_linked_entities(row)

        record = {
            "artifact_type": "logistics_incident",
            "incident_id": generate_id("INC", index),
            "created_at": choose_created_at(row, 0, 3),
            "incident_type": incident_type,
            "severity": severity,
            "status": random.choice(["open", "under_review", "resolved", "escalated"]),
            "title": f"Logistics incident for delayed order {row.get('order_id')}",
            "summary": (
                f"Order {row.get('order_id')} experienced a delivery delay of "
                f"{delay_value:.2f} days. Incident type classified as {incident_type}."
            ),
            "root_cause_hypothesis": (
                f"The delay may be related to {incident_type.replace('_', ' ')}. "
                f"Review carrier handoff, seller dispatch timing, and regional delivery capacity."
            ),
            "recommended_action": (
                "Check shipment timeline, contact seller if dispatch was late, "
                "and consider customer compensation if policy conditions are met."
            ),
            "linked_entities": linked_entities,
            "metadata": sanitize_record(
                {
                    "delivery_delay_days": row.get("delivery_delay_days"),
                    "customer_state": row.get("customer_state"),
                    "customer_city": row.get("customer_city"),
                    "seller_id": row.get("seller_id"),
                    "source": "synthetic_template_generator",
                }
            ),
        }

        record["document_text"] = (
            f"Logistics Incident {record['incident_id']} is linked to order "
            f"{linked_entities.get('order_id')} and seller {linked_entities.get('seller_id')}. "
            f"The customer region is {linked_entities.get('customer_state')} / "
            f"{linked_entities.get('customer_city')}. "
            f"Incident type: {incident_type}. Severity: {severity}. "
            f"Summary: {record['summary']} Recommended action: {record['recommended_action']}"
        )

        records.append(sanitize_record(record))

    return records


def generate_customer_emails(support_tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []

    for index, ticket in enumerate(support_tickets, start=1):
        linked = ticket["linked_entities"]

        subject = f"Re: {ticket['title']}"

        body = (
            f"Hello Support Team,\n\n"
            f"I am following up regarding order {linked.get('order_id')}. "
            f"My concern is related to {ticket['issue_type'].replace('_', ' ')}. "
            f"{ticket['customer_message']}\n\n"
            f"Please review this case and let me know the next steps.\n\n"
            f"Thank you."
        )

        record = {
            "artifact_type": "customer_email",
            "email_id": generate_id("EML", index),
            "ticket_id": ticket["ticket_id"],
            "created_at": ticket["created_at"],
            "direction": "inbound",
            "from_role": "customer",
            "to_role": "support",
            "subject": subject,
            "body": body,
            "linked_entities": linked,
            "metadata": {
                "issue_type": ticket["issue_type"],
                "severity": ticket["severity"],
                "channel": "email",
                "source": "synthetic_template_generator",
            },
            "document_text": (
                f"Customer Email {generate_id('EML', index)} is linked to ticket "
                f"{ticket['ticket_id']} and order {linked.get('order_id')}. "
                f"Subject: {subject}. Body: {body}"
            ),
        }

        records.append(sanitize_record(record))

    return records


def generate_warranty_claims(support_tickets: list[dict[str, Any]], max_records: int) -> list[dict[str, Any]]:
    eligible_tickets = [
        ticket
        for ticket in support_tickets
        if ticket["issue_type"] in {
            "damaged_item",
            "product_quality_complaint",
            "missing_item",
            "negative_review_escalation",
        }
    ]

    eligible_tickets = eligible_tickets[:max_records]

    records = []

    for index, ticket in enumerate(eligible_tickets, start=1):
        linked = ticket["linked_entities"]

        claim_reason = random.choice(
            [
                "product arrived damaged",
                "product stopped working shortly after delivery",
                "item quality did not match expected condition",
                "customer requested replacement or refund",
            ]
        )

        record = {
            "artifact_type": "warranty_claim",
            "claim_id": generate_id("WRN", index),
            "ticket_id": ticket["ticket_id"],
            "created_at": ticket["created_at"],
            "claim_status": random.choice(["submitted", "under_review", "approved", "rejected", "escalated"]),
            "claim_reason": claim_reason,
            "severity": ticket["severity"],
            "title": f"Warranty claim for order {linked.get('order_id')}",
            "summary": (
                f"Warranty claim linked to support ticket {ticket['ticket_id']} "
                f"for product {linked.get('product_id')} and seller {linked.get('seller_id')}."
            ),
            "requested_resolution": random.choice(["replacement", "refund", "seller investigation", "technical review"]),
            "linked_entities": linked,
            "metadata": {
                "issue_type": ticket["issue_type"],
                "category_id": linked.get("category_id"),
                "category_name_english": linked.get("category_name_english"),
                "source": "synthetic_template_generator",
            },
        }

        record["document_text"] = (
            f"Warranty Claim {record['claim_id']} is linked to ticket {ticket['ticket_id']}, "
            f"order {linked.get('order_id')}, product {linked.get('product_id')}, "
            f"seller {linked.get('seller_id')}, and category "
            f"{linked.get('category_name_english') or linked.get('category_id')}. "
            f"Claim reason: {claim_reason}. Requested resolution: {record['requested_resolution']}."
        )

        records.append(sanitize_record(record))

    return records


def fetch_categories(engine: Engine) -> pd.DataFrame:
    sql = """
    SELECT
        product_category_name,
        product_category_name_english
    FROM ecommerce.product_category_translations
    ORDER BY product_category_name;
    """

    return read_dataframe(engine, sql)


def generate_policy_documents(categories: pd.DataFrame) -> list[dict[str, Any]]:
    records = []

    for index, policy_topic in enumerate(POLICY_TOPICS, start=1):
        title = policy_topic.replace("_", " ").title()

        policy_text = (
            f"This policy defines the enterprise handling procedure for {title.lower()}. "
            f"Support agents must verify the order record, customer identity, seller involvement, "
            f"payment status, review history, and delivery timeline before making a decision. "
            f"Cases with high severity, negative review score, repeated seller issues, or late delivery "
            f"should be escalated according to the support operations workflow. "
            f"All decisions must be traceable to order, customer, product, seller, and ticket identifiers."
        )

        record = {
            "artifact_type": "policy_document",
            "document_id": generate_id("POL", index),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_topic": policy_topic,
            "title": title,
            "body": policy_text,
            "linked_entities": {
                "category_id": None,
                "category_name_english": None,
            },
            "metadata": {
                "document_scope": "global",
                "source": "synthetic_template_generator",
            },
            "document_text": f"{title}. {policy_text}",
        }

        records.append(sanitize_record(record))

    start_index = len(records) + 1

    for offset, (_, row) in enumerate(categories.iterrows(), start=0):
        category_id = row.get("product_category_name")
        category_name = normalize_text(row.get("product_category_name_english"), category_id)

        title = f"Category Support Handling Guide: {category_name}"

        body = (
            f"This category policy applies to customer support cases involving {category_name}. "
            f"Agents should review the customer's order, seller relationship, delivery delay status, "
            f"review score, warranty eligibility, and product-specific complaint history. "
            f"For damaged item or product quality complaints, agents should check whether the issue "
            f"is isolated to one order or recurring across sellers and regions."
        )

        record = {
            "artifact_type": "policy_document",
            "document_id": generate_id("POL", start_index + offset),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_topic": "category_support_policy",
            "title": title,
            "body": body,
            "linked_entities": {
                "category_id": category_id,
                "category_name_english": category_name,
            },
            "metadata": {
                "document_scope": "category",
                "category_id": category_id,
                "source": "synthetic_template_generator",
            },
            "document_text": f"{title}. {body}",
        }

        records.append(sanitize_record(record))

    return records


def generate_troubleshooting_guides(categories: pd.DataFrame) -> list[dict[str, Any]]:
    records = []

    for index, (_, row) in enumerate(categories.iterrows(), start=1):
        category_id = row.get("product_category_name")
        category_name = normalize_text(row.get("product_category_name_english"), category_id)

        title = f"Troubleshooting Guide for {category_name}"

        body = (
            f"This troubleshooting guide helps support agents investigate customer issues "
            f"related to {category_name}. "
            f"Step 1: verify the order and product identifiers. "
            f"Step 2: check seller, shipping, and delivery timeline. "
            f"Step 3: inspect review score and customer complaint text. "
            f"Step 4: determine whether the issue is delivery-related, product-quality-related, "
            f"payment-related, or warranty-related. "
            f"Step 5: recommend refund, replacement, seller escalation, or further investigation "
            f"based on the policy document."
        )

        record = {
            "artifact_type": "troubleshooting_guide",
            "guide_id": generate_id("TRB", index),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "title": title,
            "body": body,
            "linked_entities": {
                "category_id": category_id,
                "category_name_english": category_name,
            },
            "metadata": {
                "document_scope": "category",
                "category_id": category_id,
                "source": "synthetic_template_generator",
            },
            "document_text": f"{title}. {body}",
        }

        records.append(sanitize_record(record))

    return records


def generate_synthetic_data(
    output_dir: Path,
    report_path: Path,
    support_ticket_count: int,
    logistics_incident_count: int,
    warranty_claim_count: int,
    seed: int,
) -> dict[str, Any]:
    random.seed(seed)

    engine = build_postgres_engine()

    try:
        support_candidates = fetch_support_candidate_orders(
            engine=engine,
            max_records=support_ticket_count,
        )

        logistics_candidates = fetch_logistics_candidates(
            engine=engine,
            max_records=logistics_incident_count,
        )

        categories = fetch_categories(engine)

        support_tickets = generate_support_tickets(support_candidates)
        logistics_incidents = generate_logistics_incidents(logistics_candidates)
        customer_emails = generate_customer_emails(support_tickets)
        warranty_claims = generate_warranty_claims(
            support_tickets=support_tickets,
            max_records=warranty_claim_count,
        )
        policy_documents = generate_policy_documents(categories)
        troubleshooting_guides = generate_troubleshooting_guides(categories)

        artifact_sets = {
            "support_tickets": support_tickets,
            "logistics_incidents": logistics_incidents,
            "customer_emails": customer_emails,
            "warranty_claims": warranty_claims,
            "policy_documents": policy_documents,
            "troubleshooting_guides": troubleshooting_guides,
        }

        output_files = {}

        for artifact_name, records in artifact_sets.items():
            output_path = output_dir / f"{artifact_name}.jsonl"
            write_jsonl(output_path, records)
            output_files[artifact_name] = str(output_path)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "seed": seed,
            "output_dir": str(output_dir),
            "summary": {
                "overall_status": "PASS",
                "artifact_type_count": len(artifact_sets),
                "total_records": sum(len(records) for records in artifact_sets.values()),
            },
            "artifacts": {
                artifact_name: {
                    "record_count": len(records),
                    "output_file": output_files[artifact_name],
                }
                for artifact_name, records in artifact_sets.items()
            },
        }

        report_path.parent.mkdir(parents=True, exist_ok=True)

        with report_path.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2)

        return report

    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic enterprise artifacts linked to PostgreSQL ecommerce data."
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/synthetic"),
        help="Directory where synthetic JSONL files will be written.",
    )

    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/synthetic_generation_report.json"),
        help="Path to save synthetic generation report.",
    )

    parser.add_argument(
        "--support-ticket-count",
        type=int,
        default=3000,
        help="Number of synthetic support tickets to generate.",
    )

    parser.add_argument(
        "--logistics-incident-count",
        type=int,
        default=1000,
        help="Number of synthetic logistics incidents to generate.",
    )

    parser.add_argument(
        "--warranty-claim-count",
        type=int,
        default=1000,
        help="Maximum number of synthetic warranty claims to generate.",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for deterministic synthetic generation.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = generate_synthetic_data(
        output_dir=args.output_dir,
        report_path=args.report,
        support_ticket_count=args.support_ticket_count,
        logistics_incident_count=args.logistics_incident_count,
        warranty_claim_count=args.warranty_claim_count,
        seed=args.seed,
    )

    print("\nSynthetic Data Generation Completed")
    print("-----------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Artifact types: {report['summary']['artifact_type_count']}")
    print(f"Total records: {report['summary']['total_records']}")
    print(f"Report saved to: {args.report}")

    print("\nGenerated artifacts:")
    for artifact_name, artifact_info in report["artifacts"].items():
        print(
            f"{artifact_name}: "
            f"{artifact_info['record_count']} records -> "
            f"{artifact_info['output_file']}"
        )


if __name__ == "__main__":
    main()