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


EXPECTED_VIEWS = [
    "vw_order_summary",
    "vw_customer_order_history",
    "vw_seller_performance",
    "vw_product_performance",
    "vw_review_intelligence",
    "vw_payment_summary",
]


def build_engine() -> Engine:
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


def scalar_query(engine: Engine, sql: str) -> Any:
    with engine.begin() as connection:
        result = connection.execute(text(sql))
        return result.scalar()


def get_existing_views(engine: Engine) -> list[str]:
    sql = """
    SELECT table_name
    FROM information_schema.views
    WHERE table_schema = 'ecommerce'
    ORDER BY table_name;
    """

    with engine.begin() as connection:
        result = connection.execute(text(sql))
        return [row[0] for row in result]


def validate_view(engine: Engine, view_name: str) -> dict[str, Any]:
    row_count = int(
        scalar_query(
            engine,
            f"SELECT COUNT(*) FROM ecommerce.{view_name};",
        )
    )

    sample_query_status = "PASS"

    try:
        scalar_query(
            engine,
            f"SELECT 1 FROM ecommerce.{view_name} LIMIT 1;",
        )
    except Exception as exc:
        sample_query_status = f"FAIL: {exc}"

    return {
        "view_name": view_name,
        "row_count": row_count,
        "sample_query_status": sample_query_status,
        "status": "PASS" if sample_query_status == "PASS" else "FAIL",
    }


def validate_sql_views(output_path: Path) -> dict[str, Any]:
    engine = build_engine()

    existing_views = get_existing_views(engine)

    missing_views = [
        view_name
        for view_name in EXPECTED_VIEWS
        if view_name not in existing_views
    ]

    view_results = {}

    for view_name in EXPECTED_VIEWS:
        if view_name in missing_views:
            view_results[view_name] = {
                "view_name": view_name,
                "row_count": None,
                "sample_query_status": "MISSING",
                "status": "FAIL",
            }
        else:
            view_results[view_name] = validate_view(engine, view_name)

    failed_views = [
        view_name
        for view_name, result in view_results.items()
        if result["status"] != "PASS"
    ]

    expected_minimum_rows = {
        "vw_order_summary": 1,
        "vw_customer_order_history": 1,
        "vw_seller_performance": 1,
        "vw_product_performance": 1,
        "vw_review_intelligence": 1,
        "vw_payment_summary": 1,
    }

    low_row_count_views = []

    for view_name, minimum_rows in expected_minimum_rows.items():
        row_count = view_results[view_name]["row_count"]

        if row_count is not None and row_count < minimum_rows:
            low_row_count_views.append(view_name)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database_schema": "ecommerce",
        "summary": {
            "expected_view_count": len(EXPECTED_VIEWS),
            "existing_expected_views": len(EXPECTED_VIEWS) - len(missing_views),
            "missing_views": missing_views,
            "failed_views": failed_views,
            "low_row_count_views": low_row_count_views,
            "overall_status": (
                "PASS"
                if not missing_views and not failed_views and not low_row_count_views
                else "FAIL"
            ),
        },
        "views": view_results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate PostgreSQL business intelligence views."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/sql_view_validation_report.json"),
        help="Path to save SQL view validation report.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_sql_views(output_path=args.output)

    print("\nSQL View Validation Completed")
    print("-----------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Expected views: {report['summary']['expected_view_count']}")
    print(f"Existing expected views: {report['summary']['existing_expected_views']}")
    print(f"Report saved to: {args.output}")

    print("\nView checks:")
    for view_name, result in report["views"].items():
        print(
            f"{view_name}: "
            f"{result['status']}, "
            f"rows={result['row_count']}"
        )

    if report["summary"]["missing_views"]:
        print(f"Missing views: {report['summary']['missing_views']}")

    if report["summary"]["failed_views"]:
        print(f"Failed views: {report['summary']['failed_views']}")


if __name__ == "__main__":
    main()