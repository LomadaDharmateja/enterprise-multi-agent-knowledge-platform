from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j import Driver
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


BATCH_SIZE = 5000


NODE_QUERIES = {
    "Customer": """
        SELECT
            customer_id,
            customer_unique_id,
            customer_city,
            customer_state,
            customer_zip_code_prefix
        FROM ecommerce.customers;
    """,

    "Seller": """
        SELECT
            seller_id,
            seller_city,
            seller_state,
            seller_zip_code_prefix
        FROM ecommerce.sellers;
    """,

    "Category": """
        SELECT
            product_category_name AS category_id,
            product_category_name,
            product_category_name_english
        FROM ecommerce.product_category_translations
        WHERE product_category_name IS NOT NULL;
    """,

    "Product": """
        SELECT
            p.product_id,
            p.product_category_name,
            pct.product_category_name_english,
            p.product_weight_g,
            p.product_length_cm,
            p.product_height_cm,
            p.product_width_cm
        FROM ecommerce.products p
        LEFT JOIN ecommerce.product_category_translations pct
            ON p.product_category_name = pct.product_category_name;
    """,

    "Order": """
        SELECT
            order_id,
            customer_id,
            order_status,
            approval_status,
            shipment_status,
            delivery_status,
            order_purchase_timestamp,
            order_approved_at,
            order_delivered_carrier_date,
            order_delivered_customer_date,
            order_estimated_delivery_date,
            is_late_delivery,
            delivery_delay_days,
            purchase_to_delivery_days,
            item_count,
            unique_product_count,
            unique_seller_count,
            total_item_value,
            total_freight_value,
            total_order_item_value,
            payment_record_count,
            payment_type_count,
            payment_types,
            total_payment_value,
            max_payment_installments,
            review_count,
            avg_review_score,
            min_review_score,
            max_review_score,
            sentiment_labels
        FROM ecommerce.vw_order_summary;
    """,

    "OrderItem": """
        SELECT
            order_id || '::' || order_item_id AS order_item_key,
            order_id,
            order_item_id,
            product_id,
            seller_id,
            shipping_limit_date,
            price,
            freight_value
        FROM ecommerce.order_items;
    """,

    "Payment": """
        SELECT
            order_id || '::' || payment_sequential AS payment_key,
            order_id,
            payment_sequential,
            payment_type,
            payment_installments,
            payment_value
        FROM ecommerce.payments;
    """,

    "Review": """
        SELECT
            review_id || '::' || order_id AS review_key,
            review_id,
            order_id,
            review_score,
            sentiment_label,
            review_comment_title,
            review_comment_message,
            review_creation_date,
            review_answer_timestamp
        FROM ecommerce.reviews;
    """,

    "Region": """
        SELECT DISTINCT
            customer_state || '::' || customer_city AS region_id,
            customer_city AS city,
            customer_state AS state
        FROM ecommerce.customers
        WHERE customer_state IS NOT NULL
          AND customer_city IS NOT NULL

        UNION

        SELECT DISTINCT
            seller_state || '::' || seller_city AS region_id,
            seller_city AS city,
            seller_state AS state
        FROM ecommerce.sellers
        WHERE seller_state IS NOT NULL
          AND seller_city IS NOT NULL;
    """,
}


NODE_CYPHER = {
    "Customer": """
        UNWIND $rows AS row
        MERGE (n:Customer {customer_id: row.customer_id})
        SET n += row;
    """,

    "Seller": """
        UNWIND $rows AS row
        MERGE (n:Seller {seller_id: row.seller_id})
        SET n += row;
    """,

    "Category": """
        UNWIND $rows AS row
        MERGE (n:Category {category_id: row.category_id})
        SET n += row;
    """,

    "Product": """
        UNWIND $rows AS row
        MERGE (n:Product {product_id: row.product_id})
        SET n += row;
    """,

    "Order": """
        UNWIND $rows AS row
        MERGE (n:Order {order_id: row.order_id})
        SET n += row;
    """,

    "OrderItem": """
        UNWIND $rows AS row
        MERGE (n:OrderItem {order_item_key: row.order_item_key})
        SET n += row;
    """,

    "Payment": """
        UNWIND $rows AS row
        MERGE (n:Payment {payment_key: row.payment_key})
        SET n += row;
    """,

    "Review": """
        UNWIND $rows AS row
        MERGE (n:Review {review_key: row.review_key})
        SET n += row;
    """,

    "Region": """
        UNWIND $rows AS row
        MERGE (n:Region {region_id: row.region_id})
        SET n += row;
    """,
}


RELATIONSHIP_QUERIES = {
    "PLACED": """
        SELECT customer_id, order_id
        FROM ecommerce.orders;
    """,

    "CONTAINS_ITEM": """
        SELECT
            order_id,
            order_id || '::' || order_item_id AS order_item_key
        FROM ecommerce.order_items;
    """,

    "REFERENCES_PRODUCT": """
        SELECT
            order_id || '::' || order_item_id AS order_item_key,
            product_id
        FROM ecommerce.order_items;
    """,

    "SOLD_BY": """
        SELECT
            order_id || '::' || order_item_id AS order_item_key,
            seller_id
        FROM ecommerce.order_items;
    """,

    "HAS_PAYMENT": """
        SELECT
            order_id,
            order_id || '::' || payment_sequential AS payment_key
        FROM ecommerce.payments;
    """,

    "HAS_REVIEW": """
        SELECT
            order_id,
            review_id || '::' || order_id AS review_key
        FROM ecommerce.reviews;
    """,

    "BELONGS_TO_CATEGORY": """
        SELECT
            product_id,
            product_category_name AS category_id
        FROM ecommerce.products
        WHERE product_category_name IS NOT NULL;
    """,

    "CUSTOMER_LOCATED_IN": """
        SELECT
            customer_id,
            customer_state || '::' || customer_city AS region_id
        FROM ecommerce.customers
        WHERE customer_state IS NOT NULL
          AND customer_city IS NOT NULL;
    """,

    "SELLER_LOCATED_IN": """
        SELECT
            seller_id,
            seller_state || '::' || seller_city AS region_id
        FROM ecommerce.sellers
        WHERE seller_state IS NOT NULL
          AND seller_city IS NOT NULL;
    """,
}


RELATIONSHIP_CYPHER = {
    "PLACED": """
        UNWIND $rows AS row
        MATCH (c:Customer {customer_id: row.customer_id})
        MATCH (o:Order {order_id: row.order_id})
        MERGE (c)-[:PLACED]->(o);
    """,

    "CONTAINS_ITEM": """
        UNWIND $rows AS row
        MATCH (o:Order {order_id: row.order_id})
        MATCH (oi:OrderItem {order_item_key: row.order_item_key})
        MERGE (o)-[:CONTAINS_ITEM]->(oi);
    """,

    "REFERENCES_PRODUCT": """
        UNWIND $rows AS row
        MATCH (oi:OrderItem {order_item_key: row.order_item_key})
        MATCH (p:Product {product_id: row.product_id})
        MERGE (oi)-[:REFERENCES_PRODUCT]->(p);
    """,

    "SOLD_BY": """
        UNWIND $rows AS row
        MATCH (oi:OrderItem {order_item_key: row.order_item_key})
        MATCH (s:Seller {seller_id: row.seller_id})
        MERGE (oi)-[:SOLD_BY]->(s);
    """,

    "HAS_PAYMENT": """
        UNWIND $rows AS row
        MATCH (o:Order {order_id: row.order_id})
        MATCH (p:Payment {payment_key: row.payment_key})
        MERGE (o)-[:HAS_PAYMENT]->(p);
    """,

    "HAS_REVIEW": """
        UNWIND $rows AS row
        MATCH (o:Order {order_id: row.order_id})
        MATCH (r:Review {review_key: row.review_key})
        MERGE (o)-[:HAS_REVIEW]->(r);
    """,

    "BELONGS_TO_CATEGORY": """
        UNWIND $rows AS row
        MATCH (p:Product {product_id: row.product_id})
        MATCH (c:Category {category_id: row.category_id})
        MERGE (p)-[:BELONGS_TO_CATEGORY]->(c);
    """,

    "CUSTOMER_LOCATED_IN": """
        UNWIND $rows AS row
        MATCH (c:Customer {customer_id: row.customer_id})
        MATCH (r:Region {region_id: row.region_id})
        MERGE (c)-[:LOCATED_IN]->(r);
    """,

    "SELLER_LOCATED_IN": """
        UNWIND $rows AS row
        MATCH (s:Seller {seller_id: row.seller_id})
        MATCH (r:Region {region_id: row.region_id})
        MERGE (s)-[:LOCATED_IN]->(r);
    """,
}


NODE_COUNT_QUERIES = {
    "Customer": "MATCH (n:Customer) RETURN count(n) AS count;",
    "Seller": "MATCH (n:Seller) RETURN count(n) AS count;",
    "Category": "MATCH (n:Category) RETURN count(n) AS count;",
    "Product": "MATCH (n:Product) RETURN count(n) AS count;",
    "Order": "MATCH (n:Order) RETURN count(n) AS count;",
    "OrderItem": "MATCH (n:OrderItem) RETURN count(n) AS count;",
    "Payment": "MATCH (n:Payment) RETURN count(n) AS count;",
    "Review": "MATCH (n:Review) RETURN count(n) AS count;",
    "Region": "MATCH (n:Region) RETURN count(n) AS count;",
}


RELATIONSHIP_COUNT_QUERIES = {
    "PLACED": "MATCH ()-[r:PLACED]->() RETURN count(r) AS count;",
    "CONTAINS_ITEM": "MATCH ()-[r:CONTAINS_ITEM]->() RETURN count(r) AS count;",
    "REFERENCES_PRODUCT": "MATCH ()-[r:REFERENCES_PRODUCT]->() RETURN count(r) AS count;",
    "SOLD_BY": "MATCH ()-[r:SOLD_BY]->() RETURN count(r) AS count;",
    "HAS_PAYMENT": "MATCH ()-[r:HAS_PAYMENT]->() RETURN count(r) AS count;",
    "HAS_REVIEW": "MATCH ()-[r:HAS_REVIEW]->() RETURN count(r) AS count;",
    "BELONGS_TO_CATEGORY": "MATCH ()-[r:BELONGS_TO_CATEGORY]->() RETURN count(r) AS count;",
    "LOCATED_IN": "MATCH ()-[r:LOCATED_IN]->() RETURN count(r) AS count;",
}


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


def build_neo4j_driver() -> Driver:
    load_dotenv()

    neo4j_user = os.getenv("NEO4J_USERNAME", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "enterprise_neo4j_password")
    neo4j_bolt_port = os.getenv("NEO4J_BOLT_PORT", "7687")
    neo4j_uri = os.getenv("NEO4J_URI", f"bolt://localhost:{neo4j_bolt_port}")

    return GraphDatabase.driver(
        neo4j_uri,
        auth=(neo4j_user, neo4j_password),
    )


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


def sanitize_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records = []

    for record in df.to_dict(orient="records"):
        sanitized_record = {
            key: sanitize_value(value)
            for key, value in record.items()
        }
        records.append(sanitized_record)

    return records


def read_postgres_records(engine: Engine, sql: str) -> list[dict[str, Any]]:
    df = pd.read_sql(text(sql), engine)
    return sanitize_records(df)


def write_neo4j_batches(
    driver: Driver,
    cypher: str,
    records: list[dict[str, Any]],
    batch_size: int = BATCH_SIZE,
) -> int:
    total_written = 0

    with driver.session(database="neo4j") as session:
        for start in range(0, len(records), batch_size):
            batch = records[start:start + batch_size]
            session.execute_write(
                lambda tx, rows: tx.run(cypher, rows=rows).consume(),
                batch,
            )
            total_written += len(batch)

    return total_written


def clear_neo4j_graph(driver: Driver) -> None:
    with driver.session(database="neo4j") as session:
        session.execute_write(
            lambda tx: tx.run("MATCH (n) DETACH DELETE n;").consume()
        )


def load_nodes(
    postgres_engine: Engine,
    neo4j_driver: Driver,
) -> dict[str, Any]:
    results = {}

    for node_label, sql in NODE_QUERIES.items():
        records = read_postgres_records(postgres_engine, sql)
        written_count = write_neo4j_batches(
            driver=neo4j_driver,
            cypher=NODE_CYPHER[node_label],
            records=records,
        )

        results[node_label] = {
            "source_rows": len(records),
            "written_rows": written_count,
            "status": "LOADED",
        }

        print(f"Loaded nodes: {node_label} -> {written_count}")

    return results


def load_relationships(
    postgres_engine: Engine,
    neo4j_driver: Driver,
) -> dict[str, Any]:
    results = {}

    for relationship_name, sql in RELATIONSHIP_QUERIES.items():
        records = read_postgres_records(postgres_engine, sql)
        written_count = write_neo4j_batches(
            driver=neo4j_driver,
            cypher=RELATIONSHIP_CYPHER[relationship_name],
            records=records,
        )

        results[relationship_name] = {
            "source_rows": len(records),
            "written_rows": written_count,
            "status": "LOADED",
        }

        print(f"Loaded relationships: {relationship_name} -> {written_count}")

    return results


def neo4j_scalar(driver: Driver, cypher: str) -> int:
    with driver.session(database="neo4j") as session:
        result = session.run(cypher)
        return int(result.single()["count"])


def validate_graph_counts(
    neo4j_driver: Driver,
    node_results: dict[str, Any],
    relationship_results: dict[str, Any],
) -> dict[str, Any]:
    node_counts = {}

    for label, cypher in NODE_COUNT_QUERIES.items():
        node_counts[label] = neo4j_scalar(neo4j_driver, cypher)

    relationship_counts = {}

    for rel_type, cypher in RELATIONSHIP_COUNT_QUERIES.items():
        relationship_counts[rel_type] = neo4j_scalar(neo4j_driver, cypher)

    failed_node_counts = []

    for label, result in node_results.items():
        expected = result["source_rows"]
        actual = node_counts[label]

        if expected != actual:
            failed_node_counts.append({
                "label": label,
                "expected": expected,
                "actual": actual,
            })

    failed_relationship_counts = []

    for relationship_name, result in relationship_results.items():
        expected = result["source_rows"]

        if relationship_name in {"CUSTOMER_LOCATED_IN", "SELLER_LOCATED_IN"}:
            actual = relationship_counts["LOCATED_IN"]
            continue

        actual = relationship_counts[relationship_name]

        if expected != actual:
            failed_relationship_counts.append({
                "relationship": relationship_name,
                "expected": expected,
                "actual": actual,
            })

    expected_located_in = (
        relationship_results["CUSTOMER_LOCATED_IN"]["source_rows"]
        + relationship_results["SELLER_LOCATED_IN"]["source_rows"]
    )

    actual_located_in = relationship_counts["LOCATED_IN"]

    if expected_located_in != actual_located_in:
        failed_relationship_counts.append({
            "relationship": "LOCATED_IN",
            "expected": expected_located_in,
            "actual": actual_located_in,
        })

    return {
        "node_counts": node_counts,
        "relationship_counts": relationship_counts,
        "failed_node_counts": failed_node_counts,
        "failed_relationship_counts": failed_relationship_counts,
        "status": (
            "PASS"
            if not failed_node_counts and not failed_relationship_counts
            else "FAIL"
        ),
    }


def load_graph(output_path: Path, reset: bool) -> dict[str, Any]:
    postgres_engine = build_postgres_engine()
    neo4j_driver = build_neo4j_driver()

    try:
        if reset:
            print("Clearing existing Neo4j graph data...")
            clear_neo4j_graph(neo4j_driver)

        print("Loading Neo4j nodes...")
        node_results = load_nodes(postgres_engine, neo4j_driver)

        print("Loading Neo4j relationships...")
        relationship_results = load_relationships(postgres_engine, neo4j_driver)

        print("Validating Neo4j graph counts...")
        validation = validate_graph_counts(
            neo4j_driver=neo4j_driver,
            node_results=node_results,
            relationship_results=relationship_results,
        )

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reset_before_load": reset,
            "summary": {
                "overall_status": validation["status"],
                "node_label_count": len(node_results),
                "relationship_type_count": len(RELATIONSHIP_COUNT_QUERIES),
            },
            "nodes": node_results,
            "relationships": relationship_results,
            "validation": validation,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, default=str)

        return report

    finally:
        neo4j_driver.close()
        postgres_engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load PostgreSQL ecommerce data into Neo4j graph database."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/neo4j_load_report.json"),
        help="Path to save Neo4j load report.",
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing graph data before loading.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = load_graph(
        output_path=args.output,
        reset=args.reset,
    )

    print("\nNeo4j Graph Load Completed")
    print("--------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Node labels loaded: {report['summary']['node_label_count']}")
    print(f"Relationship types loaded: {report['summary']['relationship_type_count']}")
    print(f"Report saved to: {args.output}")

    print("\nNode counts:")
    for label, count in report["validation"]["node_counts"].items():
        print(f"{label}: {count}")

    print("\nRelationship counts:")
    for relationship_type, count in report["validation"]["relationship_counts"].items():
        print(f"{relationship_type}: {count}")

    if report["validation"]["failed_node_counts"]:
        print(f"Failed node counts: {report['validation']['failed_node_counts']}")

    if report["validation"]["failed_relationship_counts"]:
        print(
            f"Failed relationship counts: "
            f"{report['validation']['failed_relationship_counts']}"
        )


if __name__ == "__main__":
    main()