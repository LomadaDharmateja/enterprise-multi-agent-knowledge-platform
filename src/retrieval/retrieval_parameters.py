"""The parameter contract for the SQL and Cypher templates (M2, F-03).

The rule this module enforces:

    The allowlist constrains the SHAPE (which template is called).
    The planner provides the VALUES (which entities, filters, thresholds).
    The LLM never writes a query.

`gemini_query_planner` proposes values; nothing here trusts them. Every proposed
value is checked against the parameter allowlist for its template and against a
vocabulary read from the live database. Anything that does not resolve is DROPPED and
recorded, never passed through.

That last part is deliberate. Bind parameters make injection impossible, but they do
not stop the model inventing `product_category = "furniture"` -- a value that does not
exist in this dataset -- and silently returning zero rows. F-06 was
`standardize_columns` silently inventing missing columns; the same failure mode here
is a filter that matches nothing and an answer confidently grounded on an empty table.

Design: docs/M2_RETRIEVAL_DESIGN.md
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


# --------------------------------------------------------------------------------
# Parameter types
# --------------------------------------------------------------------------------

MAX_LIST_LENGTH = 25

# Olist entity IDs are 32-character hex. Synthetic artifacts use a prefixed form.
HEX_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PREFIXED_ID_PATTERN = re.compile(r"^(TCK|INC|POL|GDE|CLM|EML)-\d{6}$")

SEVERITY_VALUES = ["critical", "high", "medium", "low"]

ISSUE_TYPE_VALUES = [
    "delayed_delivery",
    "missing_item",
    "product_quality_complaint",
    "negative_review_escalation",
    "damaged_item",
    "payment_question",
]

INCIDENT_TYPE_VALUES = [
    "seller_dispatch_delay",
    "hub_missort",
    "weather_disruption",
    "carrier_backlog",
    "address_error",
    "customs_hold",
]

CLAIM_STATUS_VALUES = ["rejected", "approved", "pending", "withdrawn"]

POLICY_TOPIC_VALUES = [
    "late_delivery_compensation",
    "damaged_goods_returns",
    "warranty_claim_handling",
    "payment_dispute_resolution",
    "seller_performance_review",
    "escalation_and_ombudsman",
    "fragile_goods_packaging",
    "repeat_complaint_handling",
]

SENTIMENT_LABEL_VALUES = ["negative", "neutral", "positive"]

DELIVERY_STATUS_VALUES = ["delivered", "not_delivered"]

# Business English that maps onto this dataset's Portuguese-derived category slugs.
# Deliberately short: a guess that is wrong is worse than a dropped filter, because a
# dropped filter is recorded and a wrong one silently changes the evidence.
CATEGORY_SYNONYMS = {
    "furniture": "furniture_decor",
    "furnishings": "furniture_decor",
    "electronics": "computers_accessories",
    "computers": "computers_accessories",
    "phones": "telephony",
    "mobile": "telephony",
    "toys": "toys",
    "books": "books_general_interest",
    "watches": "watches_gifts",
    "beauty": "health_beauty",
    "health": "health_beauty",
    "sports": "sports_leisure",
    "garden": "garden_tools",
    "bedding": "bed_bath_table",
    "bed bath": "bed_bath_table",
}


# --------------------------------------------------------------------------------
# Vocabulary, read from the live database
# --------------------------------------------------------------------------------

_VOCABULARY_CACHE: dict[str, Any] | None = None


def load_vocabulary(engine: Engine, refresh: bool = False) -> dict[str, Any]:
    """Read the closed value sets a filter may draw from.

    Read once per process. The enum sets are code constants because they are defined
    by the generator, not by the warehouse; the open sets (categories, states) come
    from PostgreSQL so they cannot drift away from the data.
    """
    global _VOCABULARY_CACHE

    if _VOCABULARY_CACHE is not None and not refresh:
        return _VOCABULARY_CACHE

    with engine.connect() as connection:
        category_rows = connection.execute(
            text(
                "SELECT product_category_name, product_category_name_english"
                " FROM ecommerce.product_category_translations"
                " WHERE product_category_name IS NOT NULL"
            )
        ).fetchall()

        seller_states = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT DISTINCT seller_state FROM ecommerce.sellers"
                    " WHERE seller_state IS NOT NULL ORDER BY seller_state"
                )
            ).fetchall()
        ]

        customer_states = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT DISTINCT customer_state FROM ecommerce.customers"
                    " WHERE customer_state IS NOT NULL ORDER BY customer_state"
                )
            ).fetchall()
        ]

        order_statuses = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT DISTINCT order_status FROM ecommerce.orders"
                    " WHERE order_status IS NOT NULL ORDER BY order_status"
                )
            ).fetchall()
        ]

    # category_id is the Portuguese slug; it is the key that is populated on both the
    # PostgreSQL rows and the Neo4j Category nodes, so it is the canonical form.
    category_by_any_name: dict[str, str] = {}
    english_by_category_id: dict[str, str] = {}

    for portuguese, english in category_rows:
        category_by_any_name[portuguese.lower()] = portuguese

        if english:
            category_by_any_name[english.lower()] = portuguese
            english_by_category_id[portuguese] = english

    _VOCABULARY_CACHE = {
        "category_by_any_name": category_by_any_name,
        "english_by_category_id": english_by_category_id,
        "seller_states": seller_states,
        "customer_states": customer_states,
        "order_statuses": order_statuses,
        "severities": SEVERITY_VALUES,
        "issue_types": ISSUE_TYPE_VALUES,
        "incident_types": INCIDENT_TYPE_VALUES,
        "claim_statuses": CLAIM_STATUS_VALUES,
        "policy_topics": POLICY_TOPIC_VALUES,
        "sentiment_labels": SENTIMENT_LABEL_VALUES,
        "delivery_statuses": DELIVERY_STATUS_VALUES,
    }

    return _VOCABULARY_CACHE


def reset_vocabulary_cache() -> None:
    global _VOCABULARY_CACHE
    _VOCABULARY_CACHE = None


# --------------------------------------------------------------------------------
# Per-template parameter allowlists
# --------------------------------------------------------------------------------
#
# spec: (kind, default)
#   id_list           list of entity IDs, shape-checked
#   category_list     resolved to canonical category_id
#   enum_list:<name>  every member must be in vocabulary[<name>]
#   enum:<name>       single value, must be in vocabulary[<name>]
#   int / float / bool / date
#
# A key absent from a template's spec is not a valid filter for that template and is
# dropped with reason "not a parameter of <intent>".

SQL_PARAMETER_SPECS: dict[str, dict[str, tuple[str, Any]]] = {
    "seller_performance": {
        "seller_ids": ("id_list", None),
        "seller_states": ("enum_list:seller_states", None),
        "min_orders": ("int", 5),
        "max_avg_review_score": ("float", None),
        "min_late_delivery_rate": ("float", None),
    },
    "product_performance": {
        "product_categories": ("category_list", None),
        "product_ids": ("id_list", None),
        "min_orders": ("int", 1),
        "max_avg_review_score": ("float", None),
    },
    "review_intelligence": {
        "negative_only": ("bool", True),
        "max_review_score": ("int", None),
        "customer_states": ("enum_list:customer_states", None),
        "order_ids": ("id_list", None),
        "date_from": ("date", None),
        "date_to": ("date", None),
        "sentiment_labels": ("enum_list:sentiment_labels", None),
    },
    "payment_summary": {
        "order_ids": ("id_list", None),
        "customer_ids": ("id_list", None),
        "order_statuses": ("enum_list:order_statuses", None),
        "payment_type": ("text", None),
        "min_installments": ("int", None),
    },
    "customer_history": {
        "customer_unique_ids": ("id_list", None),
        "min_orders": ("int", 1),
        "min_late_delivery_orders": ("int", None),
        "max_avg_review_score": ("float", None),
    },
    "order_summary": {
        "order_ids": ("id_list", None),
        "order_statuses": ("enum_list:order_statuses", None),
        "delivery_status": ("enum:delivery_statuses", None),
        "customer_states": ("enum_list:customer_states", None),
        "date_from": ("date", None),
        "date_to": ("date", None),
        "is_late_delivery": ("bool", None),
        "min_delay_days": ("int", None),
    },
}

GRAPH_PARAMETER_SPECS: dict[str, dict[str, tuple[str, Any]]] = {
    "warranty_product_seller_paths": {
        "seller_ids": ("id_list", None),
        "category_ids": ("category_list", None),
        "severities": ("enum_list:severities", None),
        "claim_statuses": ("enum_list:claim_statuses", None),
    },
    "logistics_region_paths": {
        "seller_ids": ("id_list", None),
        "region_states": ("enum_list:customer_states", None),
        "incident_types": ("enum_list:incident_types", None),
        "severities": ("enum_list:severities", None),
        "min_delay_days": ("int", None),
    },
    "category_policy_guide_paths": {
        "category_ids": ("category_list", None),
        "policy_topics": ("enum_list:policy_topics", None),
        "seller_ids": ("id_list", None),
    },
    "seller_ticket_product_paths": {
        "seller_ids": ("id_list", None),
        "category_ids": ("category_list", None),
        "issue_types": ("enum_list:issue_types", None),
        "severities": ("enum_list:severities", None),
    },
    "customer_ticket_order_product_paths": {
        "customer_ids": ("id_list", None),
        "order_ids": ("id_list", None),
        "category_ids": ("category_list", None),
        "issue_types": ("enum_list:issue_types", None),
        "severities": ("enum_list:severities", None),
    },
}


# --------------------------------------------------------------------------------
# Sort keys
# --------------------------------------------------------------------------------
#
# A sort key is an allowlisted IDENTIFIER, not a fragment of SQL. It is bound as
# :sort_by and selected by a CASE inside one static ORDER BY, so no model-derived text
# ever enters a query string and every template stays a literal a test can freeze.

SQL_SORT_OPTIONS: dict[str, list[str]] = {
    "seller_performance": [
        "late_delivery_rate",
        "avg_review_score_worst",
        "total_item_revenue",
        "total_orders",
        "late_delivery_orders",
    ],
    "product_performance": [
        "total_product_revenue",
        "total_items_sold",
        "avg_review_score_worst",
        "review_count",
    ],
    "review_intelligence": ["review_score_worst", "review_creation_date_recent"],
    "payment_summary": ["total_payment_value", "max_payment_installments"],
    "customer_history": [
        "total_orders",
        "total_customer_payment_value",
        "late_delivery_orders",
    ],
    "order_summary": [
        "delivery_delay_days",
        "total_payment_value",
        "order_purchase_recent",
    ],
}

SQL_DEFAULT_SORT: dict[str, str] = {
    # Changed from late_delivery_orders (an absolute count, which ranks the largest
    # sellers) to a rate. See docs/M2_RETRIEVAL_DESIGN.md 1.1 -- this is the change
    # that stops complaint questions being grounded on well-rated high-volume sellers.
    "seller_performance": "late_delivery_rate",
    "product_performance": "total_product_revenue",
    "review_intelligence": "review_score_worst",
    "payment_summary": "total_payment_value",
    "customer_history": "total_orders",
    "order_summary": "delivery_delay_days",
}


# --------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------


def _looks_like_entity_id(value: str) -> bool:
    return bool(HEX_ID_PATTERN.match(value) or PREFIXED_ID_PATTERN.match(value))


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)

    return [value]


def _resolve_one(
    kind: str,
    value: Any,
    vocabulary: dict[str, Any],
) -> tuple[Any, str | None]:
    """Return (resolved_value, drop_reason). A drop_reason means: do not use it."""
    if value is None:
        return None, None

    if kind == "int":
        try:
            return int(value), None
        except (TypeError, ValueError):
            return None, "not an integer"

    if kind == "float":
        try:
            return float(value), None
        except (TypeError, ValueError):
            return None, "not a number"

    if kind == "bool":
        if isinstance(value, bool):
            return value, None

        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true", None

        return None, "not a boolean"

    if kind == "date":
        if isinstance(value, date):
            return value.isoformat(), None

        try:
            return date.fromisoformat(str(value)[:10]).isoformat(), None
        except ValueError:
            return None, "not an ISO date"

    if kind == "text":
        cleaned = str(value).strip()

        if not cleaned:
            return None, "empty string"

        return cleaned, None

    if kind.startswith("enum:"):
        allowed = vocabulary.get(kind.split(":", 1)[1], [])
        cleaned = str(value).strip()

        for candidate in allowed:
            if candidate.lower() == cleaned.lower():
                return candidate, None

        return None, f"not in vocabulary {kind.split(':', 1)[1]}"

    return None, f"unknown parameter kind {kind}"


def _resolve_list(
    kind: str,
    value: Any,
    vocabulary: dict[str, Any],
) -> tuple[list[Any] | None, list[tuple[Any, str]]]:
    """Resolve a list-valued filter. Returns (kept_or_None, [(value, reason), ...])."""
    items = _as_list(value)[:MAX_LIST_LENGTH]

    kept: list[Any] = []
    dropped: list[tuple[Any, str]] = []

    for item in items:
        if item is None:
            continue

        cleaned = str(item).strip()

        if not cleaned:
            continue

        if kind == "id_list":
            if _looks_like_entity_id(cleaned):
                kept.append(cleaned)
            else:
                dropped.append((item, "not a valid entity ID"))

            continue

        if kind == "category_list":
            lookup = vocabulary["category_by_any_name"]
            canonical = lookup.get(cleaned.lower())

            if canonical is None:
                synonym = CATEGORY_SYNONYMS.get(cleaned.lower())

                if synonym:
                    canonical = lookup.get(synonym.lower())

            if canonical is not None:
                kept.append(canonical)
            else:
                dropped.append((item, "not in category vocabulary"))

            continue

        if kind.startswith("enum_list:"):
            allowed = vocabulary.get(kind.split(":", 1)[1], [])
            match = next(
                (c for c in allowed if str(c).lower() == cleaned.lower()),
                None,
            )

            if match is not None:
                kept.append(match)
            else:
                dropped.append((item, f"not in vocabulary {kind.split(':', 1)[1]}"))

            continue

        dropped.append((item, f"unknown parameter kind {kind}"))

    # De-duplicate, preserving order.
    unique = list(dict.fromkeys(kept))

    return (unique or None), dropped


def resolve_filters(
    leg: str,
    intent: str | None,
    proposed: dict[str, Any] | None,
    vocabulary: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Check proposed filter values against the template's allowlist and vocabulary.

    Returns (accepted_filters, dropped_filters, proposed_count). `dropped_filters`
    entries are dicts so they can go straight into the plan and the trace -- a value
    that vanished with no record is the F-06 failure mode.
    """
    specs = SQL_PARAMETER_SPECS if leg == "sql" else GRAPH_PARAMETER_SPECS
    spec = specs.get(intent or "", {})

    accepted: dict[str, Any] = {}
    dropped: list[dict[str, Any]] = []
    proposed_count = 0

    for key, raw_value in (proposed or {}).items():
        if raw_value is None:
            continue

        proposed_count += 1

        if key not in spec:
            dropped.append(
                {
                    "leg": leg,
                    "key": key,
                    "value": raw_value,
                    "reason": f"not a parameter of {intent}",
                }
            )
            continue

        kind = spec[key][0]

        if kind in {"id_list", "category_list"} or kind.startswith("enum_list:"):
            kept, item_drops = _resolve_list(kind, raw_value, vocabulary)

            for bad_value, reason in item_drops:
                dropped.append(
                    {"leg": leg, "key": key, "value": bad_value, "reason": reason}
                )

            if kept:
                accepted[key] = kept

            continue

        value, reason = _resolve_one(kind, raw_value, vocabulary)

        if reason is not None:
            dropped.append(
                {"leg": leg, "key": key, "value": raw_value, "reason": reason}
            )
            continue

        if value is not None:
            accepted[key] = value

    return accepted, dropped, proposed_count


def resolve_sort_by(
    intent: str | None,
    proposed: Any,
    dropped: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return an allowlisted sort key, falling back to the template's default."""
    if intent is None:
        return None

    options = SQL_SORT_OPTIONS.get(intent, [])
    default = SQL_DEFAULT_SORT.get(intent)

    if proposed is None:
        return default

    if proposed in options:
        return proposed

    if dropped is not None:
        dropped.append(
            {
                "leg": "sql",
                "key": "sort_by",
                "value": proposed,
                "reason": f"not a sort option of {intent}",
            }
        )

    return default


def build_bind_parameters(
    leg: str,
    intent: str,
    accepted_filters: dict[str, Any],
) -> dict[str, Any]:
    """Fill every parameter the template declares, defaulting the unsupplied ones.

    The templates are static strings with a fixed parameter list, so every named
    parameter must be bound on every call -- an absent filter binds NULL, which the
    `CAST(:x AS ...) IS NULL OR ...` guard neutralises.
    """
    specs = SQL_PARAMETER_SPECS if leg == "sql" else GRAPH_PARAMETER_SPECS
    spec = specs.get(intent, {})

    parameters: dict[str, Any] = {}

    for key, (_kind, default) in spec.items():
        parameters[key] = accepted_filters.get(key, default)

    return parameters
