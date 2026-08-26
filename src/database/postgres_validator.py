from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


TABLES = [
    "customers",
    "sellers",
    "product_category_translations",
    "products",
    "orders",
    "order_items",
    "payments",
    "reviews",
]


def build_engine() -> Engine:
    load_dotenv()

    db_name = os.getenv("POSTGRES_DB", "enterprise_ai")
    db_user = os.getenv("POSTGRES_USER", "enterprise_user")
    db_password = os.getenv("POSTGRES_PASSWORD", "")
    db_port = os.getenv("POSTGRES_PORT", "5432")
    db_host = os.getenv("POSTGRES_HOST", "localhost")

    connection_url = (
        f"postgresql+psycopg2://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
    )

    return create_engine(connection_url)


def scalar_query(engine: Engine, sql: str) -> Any:
    with engine.begin() as connection:
        result = connection.execute(text(sql))
        return result.scalar()


def records_query(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        result = connection.execute(text(sql))
        return [dict(row._mapping) for row in result]


def get_row_counts(engine: Engine) -> dict[str, int]:
    counts = {}

    for table in TABLES:
        counts[table] = int(
            scalar_query(engine, f"SELECT COUNT(*) FROM ecommerce.{table};")
        )

    return counts


def get_relationship_checks(engine: Engine) -> dict[str, Any]:
    checks = {}

    relationship_queries = {
        "orders_without_customer": """
            SELECT COUNT(*)
            FROM ecommerce.orders o
            LEFT JOIN ecommerce.customers c
                ON o.customer_id = c.customer_id
            WHERE c.customer_id IS NULL;
        """,
        "order_items_without_order": """
            SELECT COUNT(*)
            FROM ecommerce.order_items oi
            LEFT JOIN ecommerce.orders o
                ON oi.order_id = o.order_id
            WHERE o.order_id IS NULL;
        """,
        "order_items_without_product": """
            SELECT COUNT(*)
            FROM ecommerce.order_items oi
            LEFT JOIN ecommerce.products p
                ON oi.product_id = p.product_id
            WHERE p.product_id IS NULL;
        """,
        "order_items_without_seller": """
            SELECT COUNT(*)
            FROM ecommerce.order_items oi
            LEFT JOIN ecommerce.sellers s
                ON oi.seller_id = s.seller_id
            WHERE s.seller_id IS NULL;
        """,
        "payments_without_order": """
            SELECT COUNT(*)
            FROM ecommerce.payments p
            LEFT JOIN ecommerce.orders o
                ON p.order_id = o.order_id
            WHERE o.order_id IS NULL;
        """,
        "reviews_without_order": """
            SELECT COUNT(*)
            FROM ecommerce.reviews r
            LEFT JOIN ecommerce.orders o
                ON r.order_id = o.order_id
            WHERE o.order_id IS NULL;
        """,
    }

    for check_name, query in relationship_queries.items():
        checks[check_name] = int(scalar_query(engine, query))

    checks["status"] = "PASS" if all(value == 0 for value in checks.values()) else "FAIL"

    return checks


def get_business_coverage_checks(engine: Engine) -> dict[str, Any]:
    return {
        "orders_without_items": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(*)
                FROM ecommerce.orders o
                LEFT JOIN ecommerce.order_items oi
                    ON o.order_id = oi.order_id
                WHERE oi.order_id IS NULL;
                """,
            )
        ),
        "orders_without_payments": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(*)
                FROM ecommerce.orders o
                LEFT JOIN ecommerce.payments p
                    ON o.order_id = p.order_id
                WHERE p.order_id IS NULL;
                """,
            )
        ),
        "orders_without_reviews": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(*)
                FROM ecommerce.orders o
                LEFT JOIN ecommerce.reviews r
                    ON o.order_id = r.order_id
                WHERE r.order_id IS NULL;
                """,
            )
        ),
        "orders_with_multiple_payments": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(*)
                FROM (
                    SELECT order_id
                    FROM ecommerce.payments
                    GROUP BY order_id
                    HAVING COUNT(*) > 1
                ) multi_payment_orders;
                """,
            )
        ),
        "orders_with_multiple_reviews": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(*)
                FROM (
                    SELECT order_id
                    FROM ecommerce.reviews
                    GROUP BY order_id
                    HAVING COUNT(*) > 1
                ) multi_review_orders;
                """,
            )
        ),
    }


def get_distribution_checks(engine: Engine) -> dict[str, Any]:
    return {
        "order_status_distribution": records_query(
            engine,
            """
            SELECT order_status, COUNT(*) AS order_count
            FROM ecommerce.orders
            GROUP BY order_status
            ORDER BY order_count DESC;
            """,
        ),
        "payment_type_distribution": records_query(
            engine,
            """
            SELECT payment_type, COUNT(*) AS payment_count
            FROM ecommerce.payments
            GROUP BY payment_type
            ORDER BY payment_count DESC;
            """,
        ),
        "review_score_distribution": records_query(
            engine,
            """
            SELECT review_score, COUNT(*) AS review_count
            FROM ecommerce.reviews
            GROUP BY review_score
            ORDER BY review_score;
            """,
        ),
        "sentiment_distribution": records_query(
            engine,
            """
            SELECT sentiment_label, COUNT(*) AS review_count
            FROM ecommerce.reviews
            GROUP BY sentiment_label
            ORDER BY review_count DESC;
            """,
        ),
    }


def get_business_metrics(engine: Engine) -> dict[str, Any]:
    return {
        "total_order_item_revenue": float(
            scalar_query(
                engine,
                """
                SELECT COALESCE(SUM(price), 0)
                FROM ecommerce.order_items;
                """,
            )
        ),
        "total_freight_value": float(
            scalar_query(
                engine,
                """
                SELECT COALESCE(SUM(freight_value), 0)
                FROM ecommerce.order_items;
                """,
            )
        ),
        "total_payment_value": float(
            scalar_query(
                engine,
                """
                SELECT COALESCE(SUM(payment_value), 0)
                FROM ecommerce.payments;
                """,
            )
        ),
        "average_review_score": float(
            scalar_query(
                engine,
                """
                SELECT COALESCE(AVG(review_score), 0)
                FROM ecommerce.reviews;
                """,
            )
        ),
        "unique_customers": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(DISTINCT customer_id)
                FROM ecommerce.customers;
                """,
            )
        ),
        "unique_customer_profiles": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(DISTINCT customer_unique_id)
                FROM ecommerce.customers;
                """,
            )
        ),
        "unique_sellers": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(DISTINCT seller_id)
                FROM ecommerce.sellers;
                """,
            )
        ),
        "unique_products": int(
            scalar_query(
                engine,
                """
                SELECT COUNT(DISTINCT product_id)
                FROM ecommerce.products;
                """,
            )
        ),
    }


def validate_postgres(output_path: Path) -> dict[str, Any]:
    engine = build_engine()

    row_counts = get_row_counts(engine)
    relationship_checks = get_relationship_checks(engine)
    business_coverage_checks = get_business_coverage_checks(engine)
    distribution_checks = get_distribution_checks(engine)
    business_metrics = get_business_metrics(engine)

    failed_sections = []

    if relationship_checks["status"] != "PASS":
        failed_sections.append("relationship_checks")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database_schema": "ecommerce",
        "summary": {
            "overall_status": "PASS" if not failed_sections else "FAIL",
            "failed_sections": failed_sections,
            "table_count": len(TABLES),
        },
        "row_counts": row_counts,
        "relationship_checks": relationship_checks,
        "business_coverage_checks": business_coverage_checks,
        "distribution_checks": distribution_checks,
        "business_metrics": business_metrics,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate loaded PostgreSQL ecommerce database."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/postgres_validation_report.json"),
        help="Path to save PostgreSQL validation report.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_postgres(output_path=args.output)

    print("\nPostgreSQL Validation Completed")
    print("-------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Tables checked: {report['summary']['table_count']}")
    print(f"Report saved to: {args.output}")

    print("\nRow counts:")
    for table_name, row_count in report["row_counts"].items():
        print(f"{table_name}: {row_count}")

    print("\nRelationship checks:")
    for check_name, value in report["relationship_checks"].items():
        print(f"{check_name}: {value}")


if __name__ == "__main__":
    main()