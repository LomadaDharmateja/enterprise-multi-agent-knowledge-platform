from __future__ import annotations

import argparse
import json
import os
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


DEFAULT_VECTOR_LIMIT = 5
DEFAULT_SQL_LIMIT = 10
DEFAULT_GRAPH_LIMIT = 10


def load_settings() -> dict[str, Any]:
    load_dotenv()

    return {
        "postgres_db": os.getenv("POSTGRES_DB", "enterprise_ai"),
        "postgres_user": os.getenv("POSTGRES_USER", "enterprise_user"),
        "postgres_password": os.getenv("POSTGRES_PASSWORD", "enterprise_password"),
        "postgres_host": os.getenv("POSTGRES_HOST", "localhost"),
        "postgres_port": os.getenv("POSTGRES_PORT", "5432"),
        "neo4j_username": os.getenv("NEO4J_USERNAME", "neo4j"),
        "neo4j_password": os.getenv("NEO4J_PASSWORD", "enterprise_neo4j_password"),
        "neo4j_bolt_port": os.getenv("NEO4J_BOLT_PORT", "7687"),
        "neo4j_uri": os.getenv("NEO4J_URI", f"bolt://localhost:{os.getenv('NEO4J_BOLT_PORT', '7687')}"),
        "qdrant_host": os.getenv("QDRANT_HOST", "localhost"),
        "qdrant_port": int(os.getenv("QDRANT_HTTP_PORT", "6333")),
        "qdrant_collection": os.getenv("QDRANT_COLLECTION", "enterprise_knowledge"),
        "embedding_model_name": os.getenv(
            "EMBEDDING_MODEL_NAME",
            "sentence-transformers/all-MiniLM-L6-v2",
        ),
    }


def build_postgres_engine(settings: dict[str, Any]) -> Engine:
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


def build_qdrant_client(settings: dict[str, Any]) -> QdrantClient:
    return QdrantClient(
        host=settings["qdrant_host"],
        port=settings["qdrant_port"],
    )


def records_query(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        result = connection.execute(text(sql), params or {})
        return [dict(row._mapping) for row in result]


def graph_query(driver: Driver, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with driver.session(database="neo4j") as session:
        result = session.run(cypher, params or {})
        return [dict(record) for record in result]


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


def sql_retrieve(engine: Engine,query: str,limit: int = DEFAULT_SQL_LIMIT,forced_intent: str | None = None,) -> dict[str, Any]:
    intent = forced_intent or detect_sql_intent(query)

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
                late_delivery_orders
            FROM ecommerce.vw_seller_performance
            ORDER BY
                late_delivery_orders DESC NULLS LAST,
                review_count DESC NULLS LAST,
                total_item_revenue DESC NULLS LAST
            LIMIT :limit;
        """,
        "product_performance": """
            SELECT
                product_id,
                product_category_name_english,
                total_orders,
                total_items_sold,
                total_product_revenue,
                avg_review_score,
                review_count
            FROM ecommerce.vw_product_performance
            ORDER BY
                review_count DESC NULLS LAST,
                total_items_sold DESC NULLS LAST
            LIMIT :limit;
        """,
        "review_intelligence": """
            SELECT
                review_id,
                order_id,
                customer_state,
                customer_city,
                review_score,
                sentiment_label,
                is_negative_review,
                has_review_comment,
                review_comment_title
            FROM ecommerce.vw_review_intelligence
            WHERE is_negative_review = TRUE
            ORDER BY review_score ASC NULLS LAST
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
            ORDER BY total_payment_value DESC NULLS LAST
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
            ORDER BY
                total_orders DESC NULLS LAST,
                total_customer_payment_value DESC NULLS LAST
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
                sentiment_labels
            FROM ecommerce.vw_order_summary
            ORDER BY
                is_late_delivery DESC NULLS LAST,
                delivery_delay_days DESC NULLS LAST,
                total_payment_value DESC NULLS LAST
            LIMIT :limit;
        """,
    }

    records = records_query(
        engine=engine,
        sql=sql_templates[intent],
        params={"limit": limit},
    )

    return {
        "source": "postgresql",
        "intent": intent,
        "record_count": len(records),
        "records": records,
    }


def graph_retrieve(driver: Driver,query: str,limit: int = DEFAULT_GRAPH_LIMIT,forced_intent: str | None = None,) -> dict[str, Any]:
    intent = forced_intent or detect_graph_intent(query)

    cypher_templates = {
        "warranty_product_seller_paths": """
            MATCH (w:WarrantyClaim)-[:CLAIM_FOR_TICKET]->(t:SupportTicket)
            MATCH (w)-[:CLAIMS_PRODUCT]->(p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            MATCH (w)-[:INVOLVES_SELLER]->(s:Seller)
            RETURN
                w.claim_id AS claim_id,
                t.ticket_id AS ticket_id,
                p.product_id AS product_id,
                c.product_category_name_english AS category,
                s.seller_id AS seller_id,
                s.seller_state AS seller_state,
                w.severity AS severity,
                w.claim_status AS claim_status
            LIMIT $limit;
        """,
        "logistics_region_paths": """
            MATCH (i:LogisticsIncident)-[:AFFECTS_ORDER]->(o:Order)
            MATCH (i)-[:OCCURRED_IN_REGION]->(r:Region)
            MATCH (i)-[:INVOLVES_SELLER]->(s:Seller)
            RETURN
                i.incident_id AS incident_id,
                i.incident_type AS incident_type,
                i.severity AS severity,
                o.order_id AS order_id,
                o.delivery_delay_days AS delivery_delay_days,
                r.state AS region_state,
                r.city AS region_city,
                s.seller_id AS seller_id
            ORDER BY o.delivery_delay_days DESC
            LIMIT $limit;
        """,
        "category_policy_guide_paths": """
            MATCH (p:PolicyDocument)-[:APPLIES_TO_CATEGORY]->(c:Category)<-[:GUIDE_FOR_CATEGORY]-(g:TroubleshootingGuide)
            RETURN
                c.category_id AS category_id,
                c.product_category_name_english AS category,
                p.document_id AS policy_document_id,
                p.title AS policy_title,
                g.guide_id AS guide_id,
                g.title AS guide_title
            LIMIT $limit;
        """,
        "seller_ticket_product_paths": """
            MATCH (s:Seller)<-[:INVOLVES_SELLER]-(t:SupportTicket)-[:MENTIONS_PRODUCT]->(p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            RETURN
                s.seller_id AS seller_id,
                s.seller_state AS seller_state,
                t.ticket_id AS ticket_id,
                t.issue_type AS issue_type,
                t.severity AS severity,
                p.product_id AS product_id,
                c.product_category_name_english AS category
            ORDER BY t.severity DESC
            LIMIT $limit;
        """,
        "customer_ticket_order_product_paths": """
            MATCH (customer:Customer)<-[:RAISED_BY]-(t:SupportTicket)-[:ABOUT_ORDER]->(o:Order)
            MATCH (t)-[:MENTIONS_PRODUCT]->(p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            RETURN
                customer.customer_id AS customer_id,
                customer.customer_state AS customer_state,
                t.ticket_id AS ticket_id,
                t.issue_type AS issue_type,
                t.severity AS severity,
                o.order_id AS order_id,
                p.product_id AS product_id,
                c.product_category_name_english AS category
            LIMIT $limit;
        """,
    }

    records = graph_query(
        driver=driver,
        cypher=cypher_templates[intent],
        params={"limit": limit},
    )

    return {
        "source": "neo4j",
        "intent": intent,
        "record_count": len(records),
        "records": records,
    }


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
        "artifact_id": payload.get("artifact_id"),
        "title": payload.get("title"),
        "issue_type": payload.get("issue_type"),
        "severity": payload.get("severity"),
        "status": payload.get("status"),
        "policy_topic": payload.get("policy_topic"),
        "customer_id": payload.get("customer_id"),
        "order_id": payload.get("order_id"),
        "product_id": payload.get("product_id"),
        "seller_id": payload.get("seller_id"),
        "category_id": payload.get("category_id"),
        "region_id": payload.get("region_id"),
        "text_preview": payload.get("text_preview"),
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

    for artifact_group in artifact_groups:
        points = qdrant_query_points(
            client=client,
            collection_name=collection_name,
            query_vector=query_vector,
            artifact_group=artifact_group,
            limit=per_group_limit,
        )

        compact_results = [compact_vector_result(point) for point in points]

        grouped_results[artifact_group] = compact_results
        total_results += len(compact_results)

    return {
        "source": "qdrant",
        "artifact_groups": artifact_groups,
        "record_count": total_results,
        "results_by_artifact_group": grouped_results,
    }


def run_hybrid_retrieval(
    query: str,
    output_path: Path,
    sql_limit: int,
    graph_limit: int,
    vector_limit: int,
    route_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = load_settings()

    postgres_engine = build_postgres_engine(settings)
    neo4j_driver = build_neo4j_driver(settings)
    qdrant_client = build_qdrant_client(settings)

    print(f"Loading embedding model: {settings['embedding_model_name']}")
    embedding_model = SentenceTransformer(settings["embedding_model_name"])
    forced_sql_intent = None
    forced_graph_intent = None
    forced_vector_groups = None

    if route_plan:
        forced_sql_intent = route_plan.get("sql_intent")
        forced_graph_intent = route_plan.get("graph_intent")
        forced_vector_groups = route_plan.get("vector_artifact_groups")

    try:
        print("Running SQL retrieval...")
        sql_results = sql_retrieve(
            engine=postgres_engine,
            query=query,
            limit=sql_limit,
            forced_intent=forced_sql_intent,
        )

        print("Running graph retrieval...")
        graph_results = graph_retrieve(
            driver=neo4j_driver,
            query=query,
            limit=graph_limit,
            forced_intent=forced_graph_intent,
        )

        print("Running vector retrieval...")
        vector_results = vector_retrieve(
            client=qdrant_client,
            model=embedding_model,
            collection_name=settings["qdrant_collection"],
            query=query,
            per_group_limit=vector_limit,
            forced_artifact_groups=forced_vector_groups,
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
            },
            "retrieval_results": {
                "sql": sql_results,
                "graph": graph_results,
                "vector": vector_results,
            },
        }

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