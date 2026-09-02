from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sentence_transformers import SentenceTransformer
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from retrieval_parameters import SQL_DEFAULT_SORT, build_bind_parameters

_OBSERVABILITY_DIR = Path(__file__).resolve().parents[2] / "src" / "observability"

if str(_OBSERVABILITY_DIR) not in sys.path:
    sys.path.append(str(_OBSERVABILITY_DIR))

from otel import retrieval_parent_span, retrieval_span, set_attributes  # noqa: E402
from resilience import (  # noqa: E402
    DependencyUnavailable,
    breaker_states,
    call_with_resilience,
)


DEFAULT_VECTOR_LIMIT = 5
DEFAULT_SQL_LIMIT = 10
DEFAULT_GRAPH_LIMIT = 10


def load_settings() -> dict[str, Any]:
    load_dotenv()

    return {
        "postgres_db": os.getenv("POSTGRES_DB", "enterprise_ai"),
        # M6: the runtime query path connects as a non-superuser, SELECT-only role.
        # AUDIT.md P2 found the runtime using the schema owner -- a superuser with
        # Create role, Create DB, Replication and Bypass RLS -- so "the runtime is
        # read-only" was enforced by nothing below the application. The loaders keep
        # POSTGRES_USER, which owns the schema and legitimately writes.
        #
        # No password default. A working credential as a source-code fallback is how
        # the audit found 17 of them; an unset variable must fail loudly instead.
        "postgres_user": os.getenv("POSTGRES_READONLY_USER")
        or os.getenv("POSTGRES_USER", "enterprise_user"),
        "postgres_password": os.getenv("POSTGRES_READONLY_PASSWORD")
        or os.getenv("POSTGRES_PASSWORD")
        or "",
        "postgres_host": os.getenv("POSTGRES_HOST", "localhost"),
        "postgres_port": os.getenv("POSTGRES_PORT", "5432"),
        "neo4j_username": os.getenv("NEO4J_USERNAME", "neo4j"),
        "neo4j_password": os.getenv("NEO4J_PASSWORD", "enterprise_neo4j_password"),
        "neo4j_bolt_port": os.getenv("NEO4J_BOLT_PORT", "7687"),
        "neo4j_uri": os.getenv("NEO4J_URI", f"bolt://localhost:{os.getenv('NEO4J_BOLT_PORT', '7687')}"),
        "qdrant_host": os.getenv("QDRANT_HOST", "localhost"),
        "qdrant_port": int(os.getenv("QDRANT_HTTP_PORT", "6333")),
        "qdrant_collection": os.getenv("QDRANT_COLLECTION", "enterprise_knowledge"),
        # M6: no default. An unset key must fail loudly, not connect unauthenticated.
        "qdrant_api_key": os.getenv("QDRANT_API_KEY"),
        # The client turns on HTTPS as soon as an api_key is supplied. Local Qdrant
        # speaks plain HTTP, so this must be explicit. In a real deployment the key
        # must travel over TLS -- set QDRANT_HTTPS=true there.
        "qdrant_https": os.getenv("QDRANT_HTTPS", "false").strip().lower()
        in {"true", "1", "yes"},
        "embedding_model_name": os.getenv(
            "EMBEDDING_MODEL_NAME",
            "sentence-transformers/all-MiniLM-L6-v2",
        ),
        "embedding_model_revision": os.getenv(
            "EMBEDDING_MODEL_REVISION",
            "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        ),
    }


def build_postgres_engine(settings: dict[str, Any]) -> Engine:
    if not settings.get("postgres_password"):
        raise RuntimeError(
            "No PostgreSQL password configured. Set POSTGRES_READONLY_PASSWORD (for the "
            "runtime read-only role) or POSTGRES_PASSWORD. Refusing to connect with an "
            "empty credential."
        )

    connection_url = (
        f"postgresql+psycopg2://{settings['postgres_user']}:{settings['postgres_password']}"
        f"@{settings['postgres_host']}:{settings['postgres_port']}/{settings['postgres_db']}"
    )

    return create_engine(connection_url)


def build_neo4j_driver(settings: dict[str, Any]) -> Driver:
    return GraphDatabase.driver(
        settings["neo4j_uri"],
        auth=(settings["neo4j_username"], settings["neo4j_password"]),
    )


# --------------------------------------------------------------------------------
# Embedding model: loaded once per process (M5 Task 1, AUDIT.md F-09)
# --------------------------------------------------------------------------------
#
# The embedding model was constructed inside run_hybrid_retrieval, which runs
# per request, so every query paid ~1.25s to re-read the weights from disk. The
# encode itself costs ~10ms. The model is immutable after construction and the
# revision is pinned, so one instance per process is safe and there is no cache
# invalidation to get wrong.

_EMBEDDING_MODELS: dict[tuple[str, str], SentenceTransformer] = {}
_EMBEDDING_LOCK = threading.Lock()


def get_embedding_model(
    settings: dict[str, Any] | None = None,
    model: SentenceTransformer | None = None,
) -> SentenceTransformer:
    """Return the process-wide model, constructing it at most once.

    An explicitly supplied model wins, so the API can hand down the instance it
    warmed at startup and a test can inject a stub.
    """
    if model is not None:
        return model

    settings = settings or load_settings()
    key = (settings["embedding_model_name"], settings["embedding_model_revision"])

    with _EMBEDDING_LOCK:
        cached = _EMBEDDING_MODELS.get(key)

        if cached is None:
            print(f"Loading embedding model: {key[0]} @ {key[1]}")
            cached = SentenceTransformer(key[0], revision=key[1])
            _EMBEDDING_MODELS[key] = cached

    return cached


def embedding_model_is_loaded(settings: dict[str, Any] | None = None) -> bool:
    settings = settings or load_settings()
    key = (settings["embedding_model_name"], settings["embedding_model_revision"])
    return key in _EMBEDDING_MODELS


def build_qdrant_client(settings: dict[str, Any]) -> QdrantClient:
    return QdrantClient(
        host=settings["qdrant_host"],
        port=settings["qdrant_port"],
        api_key=settings.get("qdrant_api_key"),
        https=settings.get("qdrant_https", False),
    )


def records_query(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Execute a read query in an explicitly read-only transaction.

    M6 Task 2, second enforcement layer. This was `engine.begin()` -- a committing
    read-write transaction -- and AUDIT.md P2 passed a row-returning INSERT through
    this helper and confirmed on a fresh connection that it committed.

    `postgresql_readonly=True` is SQLAlchemy's spelling for the PostgreSQL dialect; it
    emits `SET TRANSACTION READ ONLY`, so the server rejects a write even if the
    connecting role were somehow granted one. The role grant (Task 1) and this are
    independent: either alone stops the write, and neither relies on the other.
    """
    def run():
        with engine.connect() as connection:
            readonly = connection.execution_options(
                postgresql_readonly=True,
                postgresql_deferrable=True,
            )

            with readonly.begin():
                result = readonly.execute(text(sql), params or {})
                return [dict(row._mapping) for row in result]

    return call_with_resilience("postgres", "records_query", run)


def graph_query(driver: Driver, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Execute Cypher in a read transaction.

    M6 Task 2. This was `session.run()`, which opens an auto-commit transaction in
    WRITE access mode -- AUDIT.md P2 ran CREATE, index DDL and DELETE through it with
    no error. `execute_read` opens the transaction in READ access mode, and the server
    rejects a write with "Writing in read access mode not allowed" regardless of what
    the Cypher says.
    """
    def run():
        with driver.session(database="neo4j", default_access_mode="READ") as session:
            def read(tx):
                result = tx.run(cypher, params or {})
                return [dict(record) for record in result]

            return session.execute_read(read)

    return call_with_resilience("neo4j", "graph_query", run)


def normalize_query(query: str) -> str:
    return query.lower().strip()


def detect_sql_intent(query: str) -> str:
    q = normalize_query(query)

    if any(term in q for term in ["late delivery", "delivery delay", "logistics", "incident", "delay", "shipment"]):
        return "order_summary"

    if any(term in q for term in ["seller", "vendor"]):
        return "seller_performance"

    if any(term in q for term in ["payment", "installment", "refund", "charge"]):
        return "payment_summary"

    if any(term in q for term in ["review", "sentiment", "negative", "complaint"]):
        return "review_intelligence"

    if any(term in q for term in ["product", "category", "warranty"]):
        return "product_performance"

    if any(term in q for term in ["customer", "lifetime", "history"]):
        return "customer_history"

    return "order_summary"


def detect_graph_intent(query: str) -> str:
    q = normalize_query(query)

    if any(term in q for term in ["warranty", "claim"]):
        return "warranty_product_seller_paths"

    if any(term in q for term in ["logistics", "incident", "late delivery", "delay", "region"]):
        return "logistics_region_paths"

    if any(term in q for term in ["seller", "negative", "ticket paths", "seller ticket"]):
        return "seller_ticket_product_paths"

    if any(
        term in q
        for term in [
            "product quality",
            "quality complaint",
            "quality complaints",
            "product issue",
            "product issues",
            "customer complaint",
            "customer complaints",
            "complaint",
            "complaints",
            "support case",
            "support cases",
            "ticket",
            "tickets",
        ]
    ):
        return "customer_ticket_order_product_paths"

    if any(term in q for term in ["policy", "policies", "guide", "guidance", "troubleshooting"]):
        return "category_policy_guide_paths"

    return "customer_ticket_order_product_paths"


def detect_vector_filters(query: str) -> list[str]:
    q = normalize_query(query)

    filters = []

    if any(term in q for term in ["ticket", "support", "case", "complaint"]):
        filters.append("support_tickets")

    if any(term in q for term in ["email", "message", "communication"]):
        filters.append("customer_emails")

    if any(term in q for term in ["logistics", "incident", "carrier", "delay", "late delivery"]):
        filters.append("logistics_incidents")

    if any(term in q for term in ["warranty", "claim", "replacement", "damaged"]):
        filters.append("warranty_claims")

    if any(term in q for term in ["policy", "policies", "refund", "escalation", "rule", "rules"]):
        filters.append("policy_documents")

    if any(term in q for term in ["troubleshooting", "guide", "guidance", "procedure", "procedures", "steps"]):
        filters.append("troubleshooting_guides")

    if not filters:
        filters = [
            "support_tickets",
            "policy_documents",
            "troubleshooting_guides",
        ]

    return filters


def unavailable_leg_result(source: str, error: Exception) -> dict[str, Any]:
    """A leg whose dependency could not be reached.

    Distinct from `skipped_leg_result`: skipped means the plan did not select this
    leg, unavailable means it was selected and the dependency did not answer. The
    answer prompt must be able to tell those apart -- "not consulted" and "consulted
    but down" support different conclusions.
    """
    return {
        "source": source,
        "intent": None,
        "filters": {},
        "sort_by": None,
        "record_count": 0,
        "records": [],
        "skipped": False,
        "unavailable": True,
        "unavailable_reason": str(error),
        "unavailable_dependency": getattr(error, "dependency", source),
    }


def annotate_leg_span(span: Any, result: dict[str, Any]) -> None:
    """Put the leg's outcome on its span while the span is still open.

    Emitted inside the call rather than reconstructed afterwards, so start and end
    times bracket the actual database work and `duration_ms` is a measurement rather
    than a zero.
    """
    set_attributes(
        span,
        {
            "record_count": result.get("record_count", 0),
            "intent": result.get("intent"),
            "skipped": bool(result.get("skipped")),
            "evidence_linked_filtering_applied": bool(
                result.get("evidence_linked_filters")
            ),
            "evidence_linked_filters": result.get("evidence_linked_filters") or [],
            "evidence_link_fallback": bool(result.get("evidence_link_fallback")),
            "artifact_groups": result.get("artifact_groups") or [],
            "sort_by": result.get("sort_by"),
        },
    )


def skipped_leg_result(source: str) -> dict[str, Any]:
    """A leg the plan did not select.

    Reported explicitly rather than omitted, so the context builder and the answer
    prompt can say "this source was not used" instead of implying it was queried and
    came back empty. Falling back to the keyword detectors here would turn the crash
    on SQL-only plans into a wrong answer, which is worse.
    """
    return {
        "source": source,
        "intent": None,
        "filters": {},
        "sort_by": None,
        "record_count": 0,
        "records": [],
        "skipped": True,
    }


def sql_retrieve(
    engine: Engine,
    query: str,
    limit: int = DEFAULT_SQL_LIMIT,
    forced_intent: str | None = None,
    filters: dict[str, Any] | None = None,
    sort_by: str | None = None,
) -> dict[str, Any]:
    intent = forced_intent or detect_sql_intent(query)

    # F-03: every template below now accepts the parameters declared for it in
    # retrieval_parameters.SQL_PARAMETER_SPECS. Each is a single static string --
    # an unsupplied filter binds NULL and the CAST(:x AS ...) IS NULL guard
    # neutralises the clause, so no SQL is ever assembled at runtime.
    sql_templates = {
        "seller_performance": """
            SELECT
                seller_id,
                seller_state,
                seller_city,
                total_orders,
                total_items_sold,
                total_item_revenue,
                avg_review_score,
                review_count,
                late_delivery_orders,
                CASE WHEN total_orders > 0
                     THEN ROUND(late_delivery_orders::numeric / total_orders, 4)
                     ELSE 0 END AS late_delivery_rate
            FROM ecommerce.vw_seller_performance
            WHERE (CAST(:seller_ids AS text[]) IS NULL
                   OR seller_id = ANY(CAST(:seller_ids AS text[])))
              AND (CAST(:seller_states AS text[]) IS NULL
                   OR seller_state = ANY(CAST(:seller_states AS text[])))
              AND total_orders >= :min_orders
              AND (CAST(:max_avg_review_score AS numeric) IS NULL
                   OR avg_review_score <= CAST(:max_avg_review_score AS numeric))
              AND (CAST(:min_late_delivery_rate AS numeric) IS NULL
                   OR (CASE WHEN total_orders > 0
                            THEN late_delivery_orders::numeric / total_orders
                            ELSE 0 END) >= CAST(:min_late_delivery_rate AS numeric))
            ORDER BY
                CASE WHEN :sort_by = 'late_delivery_rate'
                     THEN (CASE WHEN total_orders > 0
                                THEN late_delivery_orders::numeric / total_orders
                                ELSE 0 END) END DESC NULLS LAST,
                CASE WHEN :sort_by = 'total_item_revenue'
                     THEN total_item_revenue END DESC NULLS LAST,
                CASE WHEN :sort_by = 'total_orders'
                     THEN total_orders END DESC NULLS LAST,
                CASE WHEN :sort_by = 'late_delivery_orders'
                     THEN late_delivery_orders END DESC NULLS LAST,
                CASE WHEN :sort_by = 'avg_review_score_worst'
                     THEN avg_review_score END ASC NULLS LAST,
                seller_id ASC
            LIMIT :limit;
        """,
        "product_performance": """
            SELECT
                product_id,
                product_category_name,
                product_category_name_english,
                total_orders,
                total_items_sold,
                total_product_revenue,
                avg_review_score,
                review_count
            FROM ecommerce.vw_product_performance
            WHERE (CAST(:product_categories AS text[]) IS NULL
                   OR product_category_name = ANY(CAST(:product_categories AS text[])))
              AND (CAST(:product_ids AS text[]) IS NULL
                   OR product_id = ANY(CAST(:product_ids AS text[])))
              AND total_orders >= :min_orders
              AND (CAST(:max_avg_review_score AS numeric) IS NULL
                   OR avg_review_score <= CAST(:max_avg_review_score AS numeric))
            ORDER BY
                CASE WHEN :sort_by = 'total_product_revenue'
                     THEN total_product_revenue END DESC NULLS LAST,
                CASE WHEN :sort_by = 'total_items_sold'
                     THEN total_items_sold END DESC NULLS LAST,
                CASE WHEN :sort_by = 'review_count'
                     THEN review_count END DESC NULLS LAST,
                CASE WHEN :sort_by = 'avg_review_score_worst'
                     THEN avg_review_score END ASC NULLS LAST,
                product_id ASC
            LIMIT :limit;
        """,
        "review_intelligence": """
            SELECT
                review_id,
                order_id,
                customer_id,
                customer_state,
                customer_city,
                review_score,
                sentiment_label,
                is_negative_review,
                has_review_comment,
                review_comment_title,
                review_creation_date
            FROM ecommerce.vw_review_intelligence
            WHERE (CAST(:negative_only AS boolean) IS NOT TRUE OR is_negative_review = TRUE)
              AND (CAST(:max_review_score AS integer) IS NULL
                   OR review_score <= CAST(:max_review_score AS integer))
              AND (CAST(:customer_states AS text[]) IS NULL
                   OR customer_state = ANY(CAST(:customer_states AS text[])))
              AND (CAST(:order_ids AS text[]) IS NULL
                   OR order_id = ANY(CAST(:order_ids AS text[])))
              AND (CAST(:date_from AS date) IS NULL
                   OR review_creation_date >= CAST(:date_from AS date))
              AND (CAST(:date_to AS date) IS NULL
                   OR review_creation_date < CAST(:date_to AS date) + INTERVAL '1 day')
              AND (CAST(:sentiment_labels AS text[]) IS NULL
                   OR sentiment_label = ANY(CAST(:sentiment_labels AS text[])))
            ORDER BY
                CASE WHEN :sort_by = 'review_score_worst'
                     THEN review_score END ASC NULLS LAST,
                CASE WHEN :sort_by = 'review_creation_date_recent'
                     THEN review_creation_date END DESC NULLS LAST,
                review_id ASC
            LIMIT :limit;
        """,
        "payment_summary": """
            SELECT
                order_id,
                customer_id,
                order_status,
                payment_record_count,
                payment_type_count,
                payment_types,
                total_payment_value,
                max_payment_installments
            FROM ecommerce.vw_payment_summary
            WHERE (CAST(:order_ids AS text[]) IS NULL
                   OR order_id = ANY(CAST(:order_ids AS text[])))
              AND (CAST(:customer_ids AS text[]) IS NULL
                   OR customer_id = ANY(CAST(:customer_ids AS text[])))
              AND (CAST(:order_statuses AS text[]) IS NULL
                   OR order_status = ANY(CAST(:order_statuses AS text[])))
              AND (CAST(:payment_type AS text) IS NULL
                   OR payment_types ILIKE '%' || CAST(:payment_type AS text) || '%')
              AND (CAST(:min_installments AS integer) IS NULL
                   OR max_payment_installments >= CAST(:min_installments AS integer))
            ORDER BY
                CASE WHEN :sort_by = 'total_payment_value'
                     THEN total_payment_value END DESC NULLS LAST,
                CASE WHEN :sort_by = 'max_payment_installments'
                     THEN max_payment_installments END DESC NULLS LAST,
                order_id ASC
            LIMIT :limit;
        """,
        "customer_history": """
            SELECT
                customer_unique_id,
                customer_record_count,
                total_orders,
                first_order_timestamp,
                last_order_timestamp,
                customer_states,
                total_customer_payment_value,
                avg_order_payment_value,
                avg_customer_review_score,
                late_delivery_orders
            FROM ecommerce.vw_customer_order_history
            WHERE (CAST(:customer_unique_ids AS text[]) IS NULL
                   OR customer_unique_id = ANY(CAST(:customer_unique_ids AS text[])))
              AND total_orders >= :min_orders
              AND (CAST(:min_late_delivery_orders AS integer) IS NULL
                   OR late_delivery_orders >= CAST(:min_late_delivery_orders AS integer))
              AND (CAST(:max_avg_review_score AS numeric) IS NULL
                   OR avg_customer_review_score <= CAST(:max_avg_review_score AS numeric))
            ORDER BY
                CASE WHEN :sort_by = 'total_orders'
                     THEN total_orders END DESC NULLS LAST,
                CASE WHEN :sort_by = 'total_customer_payment_value'
                     THEN total_customer_payment_value END DESC NULLS LAST,
                CASE WHEN :sort_by = 'late_delivery_orders'
                     THEN late_delivery_orders END DESC NULLS LAST,
                customer_unique_id ASC
            LIMIT :limit;
        """,
        "order_summary": """
            SELECT
                order_id,
                customer_id,
                customer_state,
                customer_city,
                order_status,
                delivery_status,
                is_late_delivery,
                delivery_delay_days,
                item_count,
                total_payment_value,
                avg_review_score,
                sentiment_labels,
                order_purchase_timestamp
            FROM ecommerce.vw_order_summary
            WHERE (CAST(:order_ids AS text[]) IS NULL
                   OR order_id = ANY(CAST(:order_ids AS text[])))
              AND (CAST(:order_statuses AS text[]) IS NULL
                   OR order_status = ANY(CAST(:order_statuses AS text[])))
              AND (CAST(:delivery_status AS text) IS NULL
                   OR delivery_status = CAST(:delivery_status AS text))
              AND (CAST(:customer_states AS text[]) IS NULL
                   OR customer_state = ANY(CAST(:customer_states AS text[])))
              AND (CAST(:date_from AS date) IS NULL
                   OR order_purchase_timestamp >= CAST(:date_from AS date))
              AND (CAST(:date_to AS date) IS NULL
                   OR order_purchase_timestamp < CAST(:date_to AS date) + INTERVAL '1 day')
              AND (CAST(:is_late_delivery AS boolean) IS NULL
                   OR is_late_delivery = CAST(:is_late_delivery AS boolean))
              AND (CAST(:min_delay_days AS integer) IS NULL
                   OR delivery_delay_days >= CAST(:min_delay_days AS integer))
            ORDER BY
                CASE WHEN :sort_by = 'delivery_delay_days'
                     THEN delivery_delay_days END DESC NULLS LAST,
                CASE WHEN :sort_by = 'total_payment_value'
                     THEN total_payment_value END DESC NULLS LAST,
                CASE WHEN :sort_by = 'order_purchase_recent'
                     THEN order_purchase_timestamp END DESC NULLS LAST,
                order_id ASC
            LIMIT :limit;
        """,
    }

    parameters = build_bind_parameters("sql", intent, filters or {})
    parameters["limit"] = limit
    parameters["sort_by"] = sort_by or SQL_DEFAULT_SORT.get(intent)

    records = records_query(
        engine=engine,
        sql=sql_templates[intent],
        params=parameters,
    )

    return {
        "source": "postgresql",
        "intent": intent,
        "filters": dict(filters or {}),
        "sort_by": parameters["sort_by"],
        "record_count": len(records),
        "records": records,
        "skipped": False,
    }


def graph_retrieve(
    driver: Driver,
    query: str,
    limit: int = DEFAULT_GRAPH_LIMIT,
    forced_intent: str | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = forced_intent or detect_graph_intent(query)

    # Severity is a string column. `ORDER BY t.severity DESC` sorts it alphabetically,
    # which on this vocabulary yields medium > low > high > critical -- the exact
    # inversion the rebuild plan flags. Every severity order below uses a rank CASE.
    # Three of these templates previously had no ORDER BY at all, so their "top ten"
    # was whatever the planner happened to emit; each now has a unique tiebreaker.
    cypher_templates = {
        "warranty_product_seller_paths": """
            MATCH (w:WarrantyClaim)-[:CLAIM_FOR_TICKET]->(t:SupportTicket)
            MATCH (w)-[:CLAIMS_PRODUCT]->(p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            MATCH (w)-[:INVOLVES_SELLER]->(s:Seller)
            WHERE ($seller_ids IS NULL OR s.seller_id IN $seller_ids)
              AND ($category_ids IS NULL OR c.category_id IN $category_ids)
              AND ($severities IS NULL OR w.severity IN $severities)
              AND ($claim_statuses IS NULL OR w.claim_status IN $claim_statuses)
            RETURN
                w.claim_id AS claim_id,
                t.ticket_id AS ticket_id,
                p.product_id AS product_id,
                c.category_id AS category_id,
                c.product_category_name_english AS category,
                s.seller_id AS seller_id,
                s.seller_state AS seller_state,
                w.severity AS severity,
                w.claim_status AS claim_status
            ORDER BY
                CASE w.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                WHEN 'medium' THEN 2 WHEN 'low' THEN 1
                                ELSE 0 END DESC,
                w.claim_id ASC
            LIMIT $limit;
        """,
        "logistics_region_paths": """
            MATCH (i:LogisticsIncident)-[:AFFECTS_ORDER]->(o:Order)
            MATCH (i)-[:OCCURRED_IN_REGION]->(r:Region)
            MATCH (i)-[:INVOLVES_SELLER]->(s:Seller)
            WHERE ($seller_ids IS NULL OR s.seller_id IN $seller_ids)
              AND ($region_states IS NULL OR r.state IN $region_states)
              AND ($incident_types IS NULL OR i.incident_type IN $incident_types)
              AND ($severities IS NULL OR i.severity IN $severities)
              AND ($min_delay_days IS NULL OR o.delivery_delay_days >= $min_delay_days)
            RETURN
                i.incident_id AS incident_id,
                i.incident_type AS incident_type,
                i.severity AS severity,
                o.order_id AS order_id,
                o.delivery_delay_days AS delivery_delay_days,
                r.state AS region_state,
                r.city AS region_city,
                s.seller_id AS seller_id
            ORDER BY o.delivery_delay_days DESC, i.incident_id ASC
            LIMIT $limit;
        """,
        "category_policy_guide_paths": """
            MATCH (p:PolicyDocument)-[:APPLIES_TO_CATEGORY]->(c:Category)<-[:GUIDE_FOR_CATEGORY]-(g:TroubleshootingGuide)
            WHERE ($category_ids IS NULL OR c.category_id IN $category_ids)
              AND ($policy_topics IS NULL OR p.policy_topic IN $policy_topics)
              AND ($seller_ids IS NULL
                   OR p.applicability_scope = 'all_sellers'
                   OR EXISTS {
                        MATCH (p)-[:APPLIES_TO_SELLER]->(scoped:Seller)
                        WHERE scoped.seller_id IN $seller_ids
                      })
            RETURN
                c.category_id AS category_id,
                c.product_category_name_english AS category,
                p.document_id AS policy_document_id,
                p.title AS policy_title,
                p.policy_topic AS policy_topic,
                p.applicability_scope AS applicability_scope,
                g.guide_id AS guide_id,
                g.title AS guide_title
            ORDER BY c.category_id ASC, p.document_id ASC, g.guide_id ASC
            LIMIT $limit;
        """,
        "seller_ticket_product_paths": """
            MATCH (s:Seller)<-[:INVOLVES_SELLER]-(t:SupportTicket)-[:MENTIONS_PRODUCT]->(p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            WHERE ($seller_ids IS NULL OR s.seller_id IN $seller_ids)
              AND ($category_ids IS NULL OR c.category_id IN $category_ids)
              AND ($issue_types IS NULL OR t.issue_type IN $issue_types)
              AND ($severities IS NULL OR t.severity IN $severities)
            RETURN
                s.seller_id AS seller_id,
                s.seller_state AS seller_state,
                t.ticket_id AS ticket_id,
                t.issue_type AS issue_type,
                t.severity AS severity,
                p.product_id AS product_id,
                c.category_id AS category_id,
                c.product_category_name_english AS category
            ORDER BY
                CASE t.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                WHEN 'medium' THEN 2 WHEN 'low' THEN 1
                                ELSE 0 END DESC,
                t.ticket_id ASC
            LIMIT $limit;
        """,
        "customer_ticket_order_product_paths": """
            MATCH (customer:Customer)<-[:RAISED_BY]-(t:SupportTicket)-[:ABOUT_ORDER]->(o:Order)
            MATCH (t)-[:MENTIONS_PRODUCT]->(p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            WHERE ($customer_ids IS NULL OR customer.customer_id IN $customer_ids)
              AND ($order_ids IS NULL OR o.order_id IN $order_ids)
              AND ($category_ids IS NULL OR c.category_id IN $category_ids)
              AND ($issue_types IS NULL OR t.issue_type IN $issue_types)
              AND ($severities IS NULL OR t.severity IN $severities)
            RETURN
                customer.customer_id AS customer_id,
                customer.customer_state AS customer_state,
                t.ticket_id AS ticket_id,
                t.issue_type AS issue_type,
                t.severity AS severity,
                o.order_id AS order_id,
                p.product_id AS product_id,
                c.category_id AS category_id,
                c.product_category_name_english AS category
            ORDER BY
                CASE t.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                WHEN 'medium' THEN 2 WHEN 'low' THEN 1
                                ELSE 0 END DESC,
                t.ticket_id ASC
            LIMIT $limit;
        """,
    }

    parameters = build_bind_parameters("graph", intent, filters or {})
    parameters["limit"] = limit

    records = graph_query(
        driver=driver,
        cypher=cypher_templates[intent],
        params=parameters,
    )

    return {
        "source": "neo4j",
        "intent": intent,
        "filters": dict(filters or {}),
        "record_count": len(records),
        "records": records,
        "skipped": False,
    }



# --------------------------------------------------------------------------------
# ID lookup (M3 fix: semantic search cannot fetch a document by its identifier)
# --------------------------------------------------------------------------------
#
# A11 asked for the root cause on ticket TCK-000002 and the vector leg returned
# TCK-001642, TCK-001232, TCK-002097 -- semantically similar tickets, none of them the
# one named. Same for WRN-000001, INC-000001 and GDE-000002. Embedding an ID and
# searching for nearby vectors is not a lookup; a payload filter is.

ARTIFACT_ID_PATTERNS = {
    "ticket_id": r"\bTCK-\d{6}\b",
    "incident_id": r"\bINC-\d{6}\b",
    "claim_id": r"\bWRN-\d{6}\b",
    "policy_id": r"\bPOL-\d{6}\b",
    "guide_id": r"\bGDE-\d{6}\b",
    "email_id": r"\bEML-\d{6}\b",
}


def extract_artifact_ids(query: str) -> list[tuple[str, str]]:
    """Return [(payload_key, id_value)] for every artifact ID named in the question."""
    found = []

    for key, pattern in ARTIFACT_ID_PATTERNS.items():
        for match in re.findall(pattern, query or "", flags=re.IGNORECASE):
            pair = (key, match.upper())

            if pair not in found:
                found.append(pair)

    return found


def id_lookup_points(
    client: QdrantClient,
    collection_name: str,
    key: str,
    value: str,
) -> list[Any]:
    """Fetch points whose payload carries this exact identifier."""
    query_filter = Filter(
        must=[FieldCondition(key=key, match=MatchValue(value=value))]
    )

    def run():
        points, _ = client.scroll(
            collection_name=collection_name,
            scroll_filter=query_filter,
            limit=5,
            with_payload=True,
            with_vectors=False,
        )
        return points

    return call_with_resilience("qdrant", "id_lookup", run)


def build_artifact_filter(artifact_group: str) -> Filter:
    return Filter(
        must=[
            FieldCondition(
                key="artifact_group",
                match=MatchValue(value=artifact_group),
            )
        ]
    )


def qdrant_query_points(
    client: QdrantClient,
    collection_name: str,
    query_vector: list[float],
    artifact_group: str,
    limit: int,
) -> list[Any]:
    query_filter = build_artifact_filter(artifact_group)

    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return list(response.points)

    return client.search(
        collection_name=collection_name,
        query_vector=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )


def compact_vector_result(point: Any) -> dict[str, Any]:
    payload = point.payload or {}

    return {
        "score": float(point.score),
        "artifact_group": payload.get("artifact_group"),
        "artifact_type": payload.get("artifact_type"),
        "artifact_id": payload.get("stable_document_id"),
        "title": payload.get("title"),
        "issue_type": payload.get("issue_type"),
        "severity": payload.get("severity"),
        "status": payload.get("status"),
        "policy_topic": payload.get("policy_topic"),
        # A document's own identifier, as a structured field rather than only inside
        # the prose of text_preview. Without these, the evaluator called policy IDs
        # that were demonstrably in the prompt "unsupported", because nothing in the
        # compacted evidence named them as data.
        "policy_id": payload.get("policy_id"),
        "guide_id": payload.get("guide_id"),
        "ticket_id": payload.get("ticket_id"),
        "claim_id": payload.get("claim_id"),
        "incident_id": payload.get("incident_id"),
        "email_id": payload.get("email_id"),
        "customer_id": payload.get("customer_id"),
        "order_id": payload.get("order_id"),
        "product_id": payload.get("product_id"),
        "seller_id": payload.get("seller_id"),
        "category_id": payload.get("category_id"),
        "region_id": payload.get("region_id"),
        "text_preview": payload.get("text"),
    }


def vector_retrieve(
    client: QdrantClient,
    model: SentenceTransformer,
    collection_name: str,
    query: str,
    per_group_limit: int = DEFAULT_VECTOR_LIMIT,
    forced_artifact_groups: list[str] | None = None,
) -> dict[str, Any]:
    artifact_groups = forced_artifact_groups or detect_vector_filters(query)

    query_vector = model.encode(
        query,
        normalize_embeddings=True,
    ).tolist()

    grouped_results = {}

    total_results = 0

    # An explicitly named artifact ID is a lookup, not a similarity search. Matched
    # points are prepended to their group and marked, so the answer agent sees the
    # document that was actually asked for first.
    id_hits: dict[str, list[dict[str, Any]]] = {}

    for key, value in extract_artifact_ids(query):
        matched_points = id_lookup_points(client, collection_name, key, value)

        # Owning document first, then referencing documents.
        matched_points.sort(
            key=lambda pt: not str(
                (pt.payload or {}).get("stable_document_id") or ""
            ).endswith(value)
        )

        for point in matched_points:
            payload = point.payload or {}
            group = payload.get("artifact_group")

            if not group:
                continue

            compact = compact_vector_result(
                type("P", (), {"payload": payload, "score": 1.0})()
            )
            compact["retrieval_method"] = "id_lookup"
            compact["matched_id"] = f"{key}={value}"
            # An ID can appear on more than one point: EML-000001 carries
            # ticket_id=TCK-000002 because the email is a follow-up to that ticket.
            # The document that OWNS the identifier -- whose stable_document_id ends
            # with it -- is the one that was asked for; referencing documents follow.
            compact["owns_id"] = str(
                compact.get("artifact_id") or ""
            ).endswith(value)
            id_hits.setdefault(group, []).append(compact)

            if group not in artifact_groups:
                artifact_groups = list(artifact_groups) + [group]

    for artifact_group in artifact_groups:
        points = qdrant_query_points(
            client=client,
            collection_name=collection_name,
            query_vector=query_vector,
            artifact_group=artifact_group,
            limit=per_group_limit,
        )

        compact_results = [compact_vector_result(point) for point in points]

        for result in compact_results:
            result.setdefault("retrieval_method", "semantic")

        looked_up = id_hits.get(artifact_group, [])

        if looked_up:
            matched_ids = {r["artifact_id"] for r in looked_up}
            compact_results = looked_up + [
                r for r in compact_results if r["artifact_id"] not in matched_ids
            ]

        grouped_results[artifact_group] = compact_results
        total_results += len(compact_results)

    return {
        "source": "qdrant",
        "artifact_groups": artifact_groups,
        "record_count": total_results,
        "id_lookups": [
            {"key": k, "value": v} for k, v in extract_artifact_ids(query)
        ],
        "id_lookup_hits": sum(len(v) for v in id_hits.values()),
        "results_by_artifact_group": grouped_results,
    }



# --------------------------------------------------------------------------------
# Question scope: population vs entity (M3 fix for the C43 class of failure)
# --------------------------------------------------------------------------------
#
# C43 asked "for the highest-revenue sellers, does complaint volume scale with order
# volume?". The planner correctly chose sort_by=total_item_revenue -- and then
# evidence-linked filtering restricted the query to the 5 seller_ids harvested from 5
# support-ticket hits, so "highest revenue" was computed over those 5. The top seller
# by revenue in the answer had 1,484 in revenue; the real top has 229,472.
#
# A superlative, a total, or a population statistic is a claim about the WHOLE
# population. Narrowing it to entities that happen to appear in the retrieved
# documents does not focus the answer, it falsifies it. Those questions bypass
# evidence linking on the SQL leg entirely.

SUPERLATIVE_PATTERNS = [
    r"\bhighest\b", r"\blowest\b", r"\blargest\b", r"\bsmallest\b",
    r"\bbiggest\b", r"\bmost\b", r"\bleast\b", r"\bworst\b", r"\bbest\b",
    r"\btop\s+\d+\b", r"\btop\s+(?:ten|five|three|twenty)\b",
    r"\brank\b", r"\branking\b", r"\bmaximum\b", r"\bminimum\b",
]

TOTAL_PATTERNS = [
    r"\bhow many\b", r"\btotal number\b", r"\bcount of\b", r"\bin total\b",
    r"\bevery\b", r"\ball of\b", r"\bdistinct\b", r"\bhow much\b",
]

POPULATION_STAT_PATTERNS = [
    r"\baverage\b", r"\bmean\b", r"\bmedian\b", r"\boverall\b",
    r"\bproportion\b", r"\bpercentage\b", r"\bpercent\b", r"\brate across\b",
    r"\bacross all\b", r"\bcompare\b", r"\bdistribution\b",
]

SCOPE_PATTERNS = {
    "superlative": SUPERLATIVE_PATTERNS,
    "total": TOTAL_PATTERNS,
    "population_statistic": POPULATION_STAT_PATTERNS,
}


def classify_question_scope(query: str) -> dict[str, Any]:
    """Deterministic. No model call -- this must not itself become a thing to evaluate."""
    lowered = (query or "").lower()

    matched: dict[str, list[str]] = {}

    for kind, patterns in SCOPE_PATTERNS.items():
        hits = [p for p in patterns if re.search(p, lowered)]

        if hits:
            matched[kind] = hits

    return {
        "scope": "population" if matched else "entity",
        "evidence_link_override": "bypass" if matched else None,
        "matched": {k: len(v) for k, v in matched.items()},
    }


# --------------------------------------------------------------------------------
# Evidence-linked filtering (decision D-1, docs/M2_RETRIEVAL_DESIGN.md)
# --------------------------------------------------------------------------------
#
# Sellers in this dataset have no human-readable names, only 32-hex IDs, so a
# seller filter almost never fires from a natural question. Planner extraction alone
# would differentiate questions by category, state, date and sort -- real, but it
# leaves the flagship question's SQL leg unfiltered by seller.
#
# So the vector leg runs FIRST, its top hits are mined for entity IDs, and those IDs
# are bound into the SQL and graph legs wherever the planner supplied no explicit
# entity filter of its own. The SQL leg then becomes question-dependent for every
# question, because the vector leg always is. This is also the first thing that
# spends F-01's fix: "a vector hit joins to a SQL row by entity ID" stops being a
# test assertion and becomes how retrieval works.
#
# Set EVIDENCE_LINKED_FILTERING=false to restore the independent-legs behaviour --
# retained so the delta stays measurable on the M3 evaluation set.

EVIDENCE_LINK_KEYS = ["seller_id", "order_id", "product_id", "customer_id"]

MAX_LINKED_IDS = 25

# Below this many rows, a leg narrowed by harvested IDs is treated as a subset rather
# than an answer and is re-run unfiltered. C43 returned 2 and passed the old `== 0`
# check.
EVIDENCE_LINK_MIN_ROWS = 5

# Which harvested ID feeds which template parameter. A template not listed here, or a
# key not listed for it, is never filled from retrieved evidence.
SQL_EVIDENCE_LINKS = {
    "seller_performance": {"seller_id": "seller_ids"},
    "product_performance": {"product_id": "product_ids"},
    "review_intelligence": {"order_id": "order_ids"},
    "payment_summary": {"order_id": "order_ids"},
    "order_summary": {"order_id": "order_ids"},
}

GRAPH_EVIDENCE_LINKS = {
    "warranty_product_seller_paths": {"seller_id": "seller_ids"},
    "logistics_region_paths": {"seller_id": "seller_ids"},
    "category_policy_guide_paths": {"seller_id": "seller_ids"},
    "seller_ticket_product_paths": {"seller_id": "seller_ids"},
    "customer_ticket_order_product_paths": {
        "customer_id": "customer_ids",
        "order_id": "order_ids",
    },
}


def evidence_linked_filtering_enabled(override: bool | None = None) -> bool:
    if override is not None:
        return override

    return os.getenv("EVIDENCE_LINKED_FILTERING", "true").strip().lower() not in {
        "false",
        "0",
        "no",
    }


def harvest_entity_ids(vector_results: dict[str, Any]) -> dict[str, list[str]]:
    """Collect the entity IDs the vector hits carry, in rank order."""
    harvested: dict[str, list[str]] = {key: [] for key in EVIDENCE_LINK_KEYS}

    groups = vector_results.get("results_by_artifact_group", {})

    for hits in groups.values():
        for hit in hits:
            for key in EVIDENCE_LINK_KEYS:
                value = hit.get(key)

                if value and value not in harvested[key]:
                    harvested[key].append(value)

    return {k: v[:MAX_LINKED_IDS] for k, v in harvested.items() if v}


def apply_evidence_links(
    leg: str,
    intent: str | None,
    planner_filters: dict[str, Any],
    harvested: dict[str, list[str]],
) -> tuple[dict[str, Any], list[str]]:
    """Bind harvested IDs into filters the planner left empty. Never overrides it."""
    if intent is None:
        return planner_filters, []

    links = (SQL_EVIDENCE_LINKS if leg == "sql" else GRAPH_EVIDENCE_LINKS).get(
        intent, {}
    )

    merged = dict(planner_filters)
    applied = []

    for harvest_key, parameter_name in links.items():
        if merged.get(parameter_name):
            continue

        values = harvested.get(harvest_key)

        if not values:
            continue

        merged[parameter_name] = values
        applied.append(parameter_name)

    return merged, applied


def assess_evidence_quality(
    proposed_filter_count: int,
    accepted_filter_count: int,
    dropped_filters: list[dict[str, Any]],
    leg_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Say how much to trust this evidence, deterministically.

    The dangerous case is not an error -- it is a question whose filters all failed to
    resolve, which silently degrades into the unfiltered query that F-03 was about.
    The answer prompt must be told, rather than being handed an unfiltered table that
    looks exactly like a filtered one.
    """
    reasons = []

    all_filters_dropped = proposed_filter_count > 0 and accepted_filter_count == 0

    if all_filters_dropped:
        reasons.append(
            f"all {proposed_filter_count} filter value(s) proposed for this question "
            "failed to resolve, so the queries ran unfiltered: "
            + "; ".join(
                f"{d['key']}={d['value']!r} ({d['reason']})" for d in dropped_filters
            )
        )
    elif dropped_filters:
        reasons.append(
            f"{len(dropped_filters)} filter value(s) were dropped as unresolvable: "
            + "; ".join(
                f"{d['key']}={d['value']!r} ({d['reason']})" for d in dropped_filters
            )
        )

    unavailable = [r for r in leg_results if r.get("unavailable")]

    for result in unavailable:
        reasons.append(
            f"{result['source']} was unavailable and was not consulted: "
            f"{result.get('unavailable_reason')}"
        )

    for result in leg_results:
        if result.get("unavailable"):
            continue

        if result.get("skipped"):
            reasons.append(f"{result['source']} was not selected by the plan")
            continue

        if result.get("evidence_link_fallback"):
            reasons.append(
                f"{result['source']} returned nothing for the entity IDs found in "
                "the retrieved documents, so it was re-run without them; these rows "
                "are not restricted to the entities the documents named"
            )

        if result["record_count"] == 0:
            reasons.append(f"{result['source']} returned no records")

    if unavailable:
        confidence = "degraded"
    elif all_filters_dropped:
        confidence = "low"
    elif reasons:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "confidence": confidence,
        "reasons": reasons,
        "filters_proposed": proposed_filter_count,
        "filters_accepted": accepted_filter_count,
        "filters_dropped": len(dropped_filters),
        "all_filters_dropped": all_filters_dropped,
    }


def run_hybrid_retrieval(
    query: str,
    output_path: Path,
    sql_limit: int,
    graph_limit: int,
    vector_limit: int,
    route_plan: dict[str, Any] | None = None,
    evidence_linked_filtering: bool | None = None,
    embedding_model: SentenceTransformer | None = None,
    run_id: str = "unknown",
) -> dict[str, Any]:
    settings = load_settings()

    postgres_engine = build_postgres_engine(settings)
    neo4j_driver = build_neo4j_driver(settings)
    qdrant_client = build_qdrant_client(settings)

    embedding_model = get_embedding_model(settings, embedding_model)

    plan = route_plan or {}

    forced_sql_intent = plan.get("sql_intent")
    forced_graph_intent = plan.get("graph_intent")
    forced_vector_groups = plan.get("vector_artifact_groups")

    sql_plan = plan.get("sql_plan") or {}
    graph_plan = plan.get("graph_plan") or {}

    planner_sql_filters = dict(sql_plan.get("filters") or {})
    planner_graph_filters = dict(graph_plan.get("filters") or {})
    planner_sort_by = sql_plan.get("sort_by")

    dropped_filters = list(plan.get("dropped_filters") or [])
    proposed_filter_count = int(plan.get("filters_proposed") or 0)

    # A plan with no legs at all is a refusal; the caller decides what to do with it.
    # Nothing here silently falls back to the keyword detectors, because turning a
    # crash into a confidently wrong answer is worse than the crash.
    link_enabled = evidence_linked_filtering_enabled(evidence_linked_filtering)

    try:
      with retrieval_parent_span(run_id) as _retrieval_parent:
        print("Running vector retrieval...")
        if forced_vector_groups == []:
            vector_results = {
                "source": "qdrant",
                "artifact_groups": [],
                "record_count": 0,
                "results_by_artifact_group": {},
                "skipped": True,
            }
        else:
            try:
                with retrieval_span(run_id, "vector") as _span:
                    vector_results = vector_retrieve(
                        client=qdrant_client,
                        model=embedding_model,
                        collection_name=settings["qdrant_collection"],
                        query=query,
                        per_group_limit=vector_limit,
                        forced_artifact_groups=forced_vector_groups,
                    )
                    vector_results["skipped"] = False
                    annotate_leg_span(_span, vector_results)
            except DependencyUnavailable as exc:
                print(f"  qdrant unavailable: {exc}")
                vector_results = {
                    "source": "qdrant", "artifact_groups": [], "record_count": 0,
                    "results_by_artifact_group": {}, "skipped": False,
                    "unavailable": True, "unavailable_reason": str(exc),
                    "unavailable_dependency": "qdrant",
                }

        harvested = harvest_entity_ids(vector_results) if link_enabled else {}

        # A superlative/total/population question is a claim about the whole
        # population; narrowing it to harvested IDs falsifies it (C43).
        scope = classify_question_scope(query)
        sql_bypass = (
            (sql_plan.get("evidence_link_override") or scope["evidence_link_override"])
            == "bypass"
        )

        sql_filters, sql_linked = apply_evidence_links(
            "sql", forced_sql_intent, planner_sql_filters,
            {} if sql_bypass else harvested,
        )
        graph_filters, graph_linked = apply_evidence_links(
            "graph", forced_graph_intent, planner_graph_filters, harvested
        )

        print("Running SQL retrieval...")
        if forced_sql_intent is None:
            sql_results = skipped_leg_result("postgresql")
        else:
            try:
                with retrieval_span(run_id, "sql") as sql_span:
                    sql_results = sql_retrieve(
                        engine=postgres_engine,
                        query=query,
                        limit=sql_limit,
                        forced_intent=forced_sql_intent,
                        filters=sql_filters,
                        sort_by=planner_sort_by,
                    )
                    sql_results["evidence_linked_filters"] = sql_linked

                    # Evidence linking is an enrichment the question did not ask for,
                    # so it must never be the reason a leg comes back empty. Two
                    # triggers, not one: `== 0` missed C43, which returned 2 rows from
                    # a 5-seller harvested set and looked like a real answer.
                    filtered_count = sql_results["record_count"]

                    if sql_linked and filtered_count < EVIDENCE_LINK_MIN_ROWS:
                        sql_results = sql_retrieve(
                            engine=postgres_engine,
                            query=query,
                            limit=sql_limit,
                            forced_intent=forced_sql_intent,
                            filters=planner_sql_filters,
                            sort_by=planner_sort_by,
                        )
                        sql_results["evidence_linked_filters"] = []
                        sql_results["evidence_link_fallback"] = True
                        sql_results["evidence_link_filtered_count"] = filtered_count
                        sql_results["evidence_link_unfiltered_count"] = sql_results[
                            "record_count"
                        ]
                        sql_results["confidence_flag"] = (
                            "filtered_subset_warning" if filtered_count > 0 else None
                        )

                    # Annotated unconditionally, and inside the span, so the attributes
                    # describe the leg that actually ran -- fallback included.
                    annotate_leg_span(sql_span, sql_results)
            except DependencyUnavailable as exc:
                print(f"  postgresql unavailable: {exc}")
                sql_results = unavailable_leg_result("postgresql", exc)

        print("Running graph retrieval...")
        if forced_graph_intent is None:
            graph_results = skipped_leg_result("neo4j")
        else:
            try:
                with retrieval_span(run_id, "graph") as graph_span:
                    graph_results = graph_retrieve(
                        driver=neo4j_driver,
                        query=query,
                        limit=graph_limit,
                        forced_intent=forced_graph_intent,
                        filters=graph_filters,
                    )
                    graph_results["evidence_linked_filters"] = graph_linked

                    graph_filtered_count = graph_results["record_count"]

                    if graph_linked and graph_filtered_count < EVIDENCE_LINK_MIN_ROWS:
                        graph_results = graph_retrieve(
                            driver=neo4j_driver,
                            query=query,
                            limit=graph_limit,
                            forced_intent=forced_graph_intent,
                            filters=planner_graph_filters,
                        )
                        graph_results["evidence_linked_filters"] = []
                        graph_results["evidence_link_fallback"] = True
                        graph_results["evidence_link_filtered_count"] = graph_filtered_count
                        graph_results["evidence_link_unfiltered_count"] = graph_results[
                            "record_count"
                        ]
                        graph_results["confidence_flag"] = (
                            "filtered_subset_warning"
                            if graph_filtered_count > 0
                            else None
                        )

                    annotate_leg_span(graph_span, graph_results)
            except DependencyUnavailable as exc:
                print(f"  neo4j unavailable: {exc}")
                graph_results = unavailable_leg_result("neo4j", exc)

        accepted_filter_count = len(planner_sql_filters) + len(planner_graph_filters)

        evidence_quality = assess_evidence_quality(
            proposed_filter_count=proposed_filter_count,
            accepted_filter_count=accepted_filter_count,
            dropped_filters=dropped_filters,
            leg_results=[sql_results, graph_results, vector_results],
        )

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "route_plan": route_plan,
            "summary": {
                "overall_status": "PASS",
                "sql_records": sql_results["record_count"],
                "graph_records": graph_results["record_count"],
                "vector_records": vector_results["record_count"],
                "evidence_linked_filtering": link_enabled,
                "question_scope": scope,
                "sql_evidence_link_bypassed": sql_bypass,
                "evidence_linked_ids": {k: len(v) for k, v in harvested.items()},
                "evidence_quality": evidence_quality,
                "unavailable_legs": [
                    {
                        "source": leg["source"],
                        "dependency": leg.get("unavailable_dependency"),
                        "reason": leg.get("unavailable_reason"),
                    }
                    for leg in (sql_results, graph_results, vector_results)
                    if leg.get("unavailable")
                ],
                "circuit_breakers": breaker_states(),
            },
            "retrieval_results": {
                "sql": sql_results,
                "graph": graph_results,
                "vector": vector_results,
            },
        }

        set_attributes(
            _retrieval_parent,
            {
                "sql_records": sql_results["record_count"],
                "graph_records": graph_results["record_count"],
                "vector_records": vector_results["record_count"],
                "total_records": (
                    sql_results["record_count"]
                    + graph_results["record_count"]
                    + vector_results["record_count"]
                ),
                "unavailable_legs": [
                    leg["source"]
                    for leg in (sql_results, graph_results, vector_results)
                    if leg.get("unavailable")
                ],
                "evidence_confidence": evidence_quality.get("confidence"),
                "question_scope": scope["scope"],
            },
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, default=str)

        return report

    finally:
        postgres_engine.dispose()
        neo4j_driver.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hybrid retrieval across PostgreSQL, Neo4j, and Qdrant."
    )

    parser.add_argument(
        "--query",
        type=str,
        required=True,
        help="Natural-language business query.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/hybrid_retrieval_report.json"),
        help="Path to save hybrid retrieval report.",
    )

    parser.add_argument(
        "--sql-limit",
        type=int,
        default=10,
        help="Number of SQL records to return.",
    )

    parser.add_argument(
        "--graph-limit",
        type=int,
        default=10,
        help="Number of graph records to return.",
    )

    parser.add_argument(
        "--vector-limit",
        type=int,
        default=5,
        help="Number of vector results per artifact group.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = run_hybrid_retrieval(
        query=args.query,
        output_path=args.output,
        sql_limit=args.sql_limit,
        graph_limit=args.graph_limit,
        vector_limit=args.vector_limit,
    )

    print("\nHybrid Retrieval Completed")
    print("--------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Query: {report['query']}")
    print(f"SQL records: {report['summary']['sql_records']}")
    print(f"Graph records: {report['summary']['graph_records']}")
    print(f"Vector records: {report['summary']['vector_records']}")
    print(f"Report saved to: {args.output}")

    print("\nSelected intents:")
    print(f"SQL intent: {report['retrieval_results']['sql']['intent']}")
    print(f"Graph intent: {report['retrieval_results']['graph']['intent']}")
    print(
        "Vector artifact groups: "
        f"{report['retrieval_results']['vector']['artifact_groups']}"
    )


if __name__ == "__main__":
    main()