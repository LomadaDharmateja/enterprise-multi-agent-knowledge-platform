from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j import Driver


NODE_LABELS = [
    "Customer",
    "Seller",
    "Category",
    "Product",
    "Order",
    "OrderItem",
    "Payment",
    "Review",
    "Region",
]


RELATIONSHIP_TYPES = [
    "PLACED",
    "CONTAINS_ITEM",
    "REFERENCES_PRODUCT",
    "SOLD_BY",
    "HAS_PAYMENT",
    "HAS_REVIEW",
    "BELONGS_TO_CATEGORY",
    "LOCATED_IN",
]


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


def run_query(driver: Driver, cypher: str) -> list[dict[str, Any]]:
    with driver.session(database="neo4j") as session:
        result = session.run(cypher)
        return [dict(record) for record in result]


def scalar_query(driver: Driver, cypher: str) -> int:
    records = run_query(driver, cypher)

    if not records:
        return 0

    return int(next(iter(records[0].values())))


def get_node_counts(driver: Driver) -> dict[str, int]:
    counts = {}

    for label in NODE_LABELS:
        counts[label] = scalar_query(
            driver,
            f"MATCH (n:{label}) RETURN count(n) AS count;",
        )

    return counts


def get_relationship_counts(driver: Driver) -> dict[str, int]:
    counts = {}

    for relationship_type in RELATIONSHIP_TYPES:
        counts[relationship_type] = scalar_query(
            driver,
            f"MATCH ()-[r:{relationship_type}]->() RETURN count(r) AS count;",
        )

    return counts


def get_orphan_checks(driver: Driver) -> dict[str, int]:
    checks = {
        "orders_without_customer": """
            MATCH (o:Order)
            WHERE NOT EXISTS {
                MATCH (:Customer)-[:PLACED]->(o)
            }
            RETURN count(o) AS count;
        """,
        "order_items_without_order": """
            MATCH (oi:OrderItem)
            WHERE NOT EXISTS {
                MATCH (:Order)-[:CONTAINS_ITEM]->(oi)
            }
            RETURN count(oi) AS count;
        """,
        "order_items_without_product": """
            MATCH (oi:OrderItem)
            WHERE NOT EXISTS {
                MATCH (oi)-[:REFERENCES_PRODUCT]->(:Product)
            }
            RETURN count(oi) AS count;
        """,
        "order_items_without_seller": """
            MATCH (oi:OrderItem)
            WHERE NOT EXISTS {
                MATCH (oi)-[:SOLD_BY]->(:Seller)
            }
            RETURN count(oi) AS count;
        """,
        "payments_without_order": """
            MATCH (p:Payment)
            WHERE NOT EXISTS {
                MATCH (:Order)-[:HAS_PAYMENT]->(p)
            }
            RETURN count(p) AS count;
        """,
        "reviews_without_order": """
            MATCH (r:Review)
            WHERE NOT EXISTS {
                MATCH (:Order)-[:HAS_REVIEW]->(r)
            }
            RETURN count(r) AS count;
        """,
        "products_without_category": """
            MATCH (p:Product)
            WHERE NOT EXISTS {
                MATCH (p)-[:BELONGS_TO_CATEGORY]->(:Category)
            }
            RETURN count(p) AS count;
        """,
        "customers_without_region": """
            MATCH (c:Customer)
            WHERE NOT EXISTS {
                MATCH (c)-[:LOCATED_IN]->(:Region)
            }
            RETURN count(c) AS count;
        """,
        "sellers_without_region": """
            MATCH (s:Seller)
            WHERE NOT EXISTS {
                MATCH (s)-[:LOCATED_IN]->(:Region)
            }
            RETURN count(s) AS count;
        """,
    }

    return {
        check_name: scalar_query(driver, cypher)
        for check_name, cypher in checks.items()
    }


def get_business_path_checks(driver: Driver) -> dict[str, Any]:
    checks = {
        "customer_order_payment_paths": """
            MATCH (:Customer)-[:PLACED]->(:Order)-[:HAS_PAYMENT]->(:Payment)
            RETURN count(*) AS count;
        """,
        "customer_order_review_paths": """
            MATCH (:Customer)-[:PLACED]->(:Order)-[:HAS_REVIEW]->(:Review)
            RETURN count(*) AS count;
        """,
        "seller_product_paths": """
            MATCH (:Seller)<-[:SOLD_BY]-(:OrderItem)-[:REFERENCES_PRODUCT]->(:Product)
            RETURN count(*) AS count;
        """,
        "negative_review_product_seller_paths": """
            MATCH (r:Review)<-[:HAS_REVIEW]-(o:Order)-[:CONTAINS_ITEM]->(oi:OrderItem)-[:REFERENCES_PRODUCT]->(:Product)
            MATCH (oi)-[:SOLD_BY]->(:Seller)
            WHERE r.review_score <= 2
            RETURN count(*) AS count;
        """,
        "customer_region_order_paths": """
            MATCH (c:Customer)-[:LOCATED_IN]->(:Region)
            MATCH (c)-[:PLACED]->(:Order)
            RETURN count(*) AS count;
        """,
        "seller_region_order_item_paths": """
            MATCH (s:Seller)-[:LOCATED_IN]->(:Region)
            MATCH (:OrderItem)-[:SOLD_BY]->(s)
            RETURN count(*) AS count;
        """,
        "late_delivery_review_paths": """
            MATCH (o:Order)-[:HAS_REVIEW]->(:Review)
            WHERE o.is_late_delivery = true
            RETURN count(*) AS count;
        """,
    }

    return {
        check_name: scalar_query(driver, cypher)
        for check_name, cypher in checks.items()
    }


def get_sample_business_queries(driver: Driver) -> dict[str, list[dict[str, Any]]]:
    return {
        "top_negative_review_product_categories": run_query(
            driver,
            """
            MATCH (r:Review)<-[:HAS_REVIEW]-(o:Order)-[:CONTAINS_ITEM]->(oi:OrderItem)-[:REFERENCES_PRODUCT]->(p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            WHERE r.review_score <= 2
            RETURN
                c.product_category_name_english AS category,
                count(r) AS negative_review_count
            ORDER BY negative_review_count DESC
            LIMIT 10;
            """,
        ),
        "top_sellers_by_negative_reviews": run_query(
            driver,
            """
            MATCH (r:Review)<-[:HAS_REVIEW]-(o:Order)-[:CONTAINS_ITEM]->(oi:OrderItem)-[:SOLD_BY]->(s:Seller)
            WHERE r.review_score <= 2
            RETURN
                s.seller_id AS seller_id,
                s.seller_state AS seller_state,
                count(r) AS negative_review_count
            ORDER BY negative_review_count DESC
            LIMIT 10;
            """,
        ),
        "top_regions_by_late_delivery_orders": run_query(
            driver,
            """
            MATCH (c:Customer)-[:LOCATED_IN]->(region:Region)
            MATCH (c)-[:PLACED]->(o:Order)
            WHERE o.is_late_delivery = true
            RETURN
                region.state AS state,
                region.city AS city,
                count(o) AS late_delivery_orders
            ORDER BY late_delivery_orders DESC
            LIMIT 10;
            """,
        ),
        "top_product_categories_by_revenue_signal": run_query(
            driver,
            """
            MATCH (p:Product)-[:BELONGS_TO_CATEGORY]->(c:Category)
            MATCH (:OrderItem)-[:REFERENCES_PRODUCT]->(p)
            RETURN
                c.product_category_name_english AS category,
                count(p) AS product_item_connections
            ORDER BY product_item_connections DESC
            LIMIT 10;
            """,
        ),
    }


def validate_graph(output_path: Path) -> dict[str, Any]:
    driver = build_neo4j_driver()

    try:
        node_counts = get_node_counts(driver)
        relationship_counts = get_relationship_counts(driver)
        orphan_checks = get_orphan_checks(driver)
        business_path_checks = get_business_path_checks(driver)
        sample_business_queries = get_sample_business_queries(driver)

        failed_orphan_checks = [
            check_name
            for check_name, value in orphan_checks.items()
            if value != 0
        ]

        failed_path_checks = [
            check_name
            for check_name, value in business_path_checks.items()
            if value == 0
        ]

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": "neo4j",
            "summary": {
                "overall_status": (
                    "PASS"
                    if not failed_orphan_checks and not failed_path_checks
                    else "FAIL"
                ),
                "node_label_count": len(NODE_LABELS),
                "relationship_type_count": len(RELATIONSHIP_TYPES),
                "failed_orphan_checks": failed_orphan_checks,
                "failed_path_checks": failed_path_checks,
            },
            "node_counts": node_counts,
            "relationship_counts": relationship_counts,
            "orphan_checks": orphan_checks,
            "business_path_checks": business_path_checks,
            "sample_business_queries": sample_business_queries,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, default=str)

        return report

    finally:
        driver.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Neo4j graph readiness for business reasoning."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/neo4j_validation_report.json"),
        help="Path to save Neo4j validation report.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_graph(output_path=args.output)

    print("\nNeo4j Graph Validation Completed")
    print("--------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Node labels checked: {report['summary']['node_label_count']}")
    print(f"Relationship types checked: {report['summary']['relationship_type_count']}")
    print(f"Report saved to: {args.output}")

    print("\nOrphan checks:")
    for check_name, value in report["orphan_checks"].items():
        print(f"{check_name}: {value}")

    print("\nBusiness path checks:")
    for check_name, value in report["business_path_checks"].items():
        print(f"{check_name}: {value}")

    if report["summary"]["failed_orphan_checks"]:
        print(f"\nFailed orphan checks: {report['summary']['failed_orphan_checks']}")

    if report["summary"]["failed_path_checks"]:
        print(f"\nFailed path checks: {report['summary']['failed_path_checks']}")


if __name__ == "__main__":
    main()