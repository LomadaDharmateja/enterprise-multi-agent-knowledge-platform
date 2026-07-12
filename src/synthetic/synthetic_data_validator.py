
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


EXPECTED_FILES = {
    "support_tickets": "support_tickets.jsonl",
    "logistics_incidents": "logistics_incidents.jsonl",
    "customer_emails": "customer_emails.jsonl",
    "warranty_claims": "warranty_claims.jsonl",
    "policy_documents": "policy_documents.jsonl",
    "troubleshooting_guides": "troubleshooting_guides.jsonl",
}


ID_FIELDS = {
    "support_tickets": "ticket_id",
    "logistics_incidents": "incident_id",
    "customer_emails": "email_id",
    "warranty_claims": "claim_id",
    "policy_documents": "document_id",
    "troubleshooting_guides": "guide_id",
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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []

    if not path.exists():
        return records

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path} at line {line_number}: {exc}") from exc

    return records


def fetch_set(engine: Engine, sql: str) -> set[str]:
    with engine.begin() as connection:
        result = connection.execute(text(sql))
        return {str(row[0]) for row in result if row[0] is not None}


def load_reference_sets(engine: Engine) -> dict[str, set[str]]:
    return {
        "customer_ids": fetch_set(engine, "SELECT customer_id FROM ecommerce.customers;"),
        "order_ids": fetch_set(engine, "SELECT order_id FROM ecommerce.orders;"),
        "product_ids": fetch_set(engine, "SELECT product_id FROM ecommerce.products;"),
        "seller_ids": fetch_set(engine, "SELECT seller_id FROM ecommerce.sellers;"),
        "review_ids": fetch_set(engine, "SELECT review_id FROM ecommerce.reviews;"),
        "category_ids": fetch_set(
            engine,
            "SELECT product_category_name FROM ecommerce.product_category_translations;",
        ),
        "region_ids": fetch_set(
            engine,
            """
            SELECT customer_state || '::' || customer_city AS region_id
            FROM ecommerce.customers
            WHERE customer_state IS NOT NULL
              AND customer_city IS NOT NULL

            UNION

            SELECT seller_state || '::' || seller_city AS region_id
            FROM ecommerce.sellers
            WHERE seller_state IS NOT NULL
              AND seller_city IS NOT NULL;
            """,
        ),
    }


def get_nested(record: dict[str, Any], field: str) -> Any:
    current: Any = record

    for part in field.split("."):
        if not isinstance(current, dict):
            return None

        current = current.get(part)

    return current


def has_text(value: Any) -> bool:
    if value is None:
        return False

    return str(value).strip() != ""


def count_duplicate_values(records: list[dict[str, Any]], id_field: str) -> int:
    values = [record.get(id_field) for record in records if has_text(record.get(id_field))]
    counts = Counter(values)

    return sum(count for count in counts.values() if count > 1)


def validate_basic_structure(
    artifact_name: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    id_field = ID_FIELDS[artifact_name]

    missing_id = 0
    missing_artifact_type = 0
    missing_document_text = 0
    duplicate_id_rows = count_duplicate_values(records, id_field)

    for record in records:
        if not has_text(record.get(id_field)):
            missing_id += 1

        if not has_text(record.get("artifact_type")):
            missing_artifact_type += 1

        if not has_text(record.get("document_text")):
            missing_document_text += 1

    status = (
        "PASS"
        if missing_id == 0
        and missing_artifact_type == 0
        and missing_document_text == 0
        and duplicate_id_rows == 0
        else "FAIL"
    )

    return {
        "record_count": len(records),
        "id_field": id_field,
        "missing_id": missing_id,
        "missing_artifact_type": missing_artifact_type,
        "missing_document_text": missing_document_text,
        "duplicate_id_rows": duplicate_id_rows,
        "status": status,
    }


def validate_reference_field(
    records: list[dict[str, Any]],
    field_path: str,
    reference_values: set[str],
    required: bool,
) -> dict[str, Any]:
    missing_values = 0
    invalid_values = 0
    checked_values = 0

    for record in records:
        value = get_nested(record, field_path)

        if not has_text(value):
            if required:
                missing_values += 1
            continue

        checked_values += 1

        if str(value) not in reference_values:
            invalid_values += 1

    status = (
        "PASS"
        if missing_values == 0 and invalid_values == 0
        else "FAIL"
    )

    return {
        "field": field_path,
        "required": required,
        "checked_values": checked_values,
        "missing_values": missing_values,
        "invalid_values": invalid_values,
        "status": status,
    }


def validate_ticket_references(
    records: list[dict[str, Any]],
    reference_sets: dict[str, set[str]],
) -> dict[str, Any]:
    checks = {
        "customer_id": validate_reference_field(
            records,
            "linked_entities.customer_id",
            reference_sets["customer_ids"],
            required=True,
        ),
        "order_id": validate_reference_field(
            records,
            "linked_entities.order_id",
            reference_sets["order_ids"],
            required=True,
        ),
        "product_id": validate_reference_field(
            records,
            "linked_entities.product_id",
            reference_sets["product_ids"],
            required=True,
        ),
        "seller_id": validate_reference_field(
            records,
            "linked_entities.seller_id",
            reference_sets["seller_ids"],
            required=True,
        ),
        "category_id": validate_reference_field(
            records,
            "linked_entities.category_id",
            reference_sets["category_ids"],
            required=True,
        ),
        "region_id": validate_reference_field(
            records,
            "linked_entities.region_id",
            reference_sets["region_ids"],
            required=True,
        ),
        "review_id": validate_reference_field(
            records,
            "linked_entities.review_id",
            reference_sets["review_ids"],
            required=False,
        ),
    }

    failed_checks = [
        check_name
        for check_name, result in checks.items()
        if result["status"] != "PASS"
    ]

    return {
        "checks": checks,
        "failed_checks": failed_checks,
        "status": "PASS" if not failed_checks else "FAIL",
    }


def validate_email_references(
    records: list[dict[str, Any]],
    support_ticket_ids: set[str],
    reference_sets: dict[str, set[str]],
) -> dict[str, Any]:
    checks = {
        "ticket_id": validate_reference_field(
            records,
            "ticket_id",
            support_ticket_ids,
            required=True,
        ),
        "order_id": validate_reference_field(
            records,
            "linked_entities.order_id",
            reference_sets["order_ids"],
            required=True,
        ),
        "customer_id": validate_reference_field(
            records,
            "linked_entities.customer_id",
            reference_sets["customer_ids"],
            required=True,
        ),
    }

    failed_checks = [
        check_name
        for check_name, result in checks.items()
        if result["status"] != "PASS"
    ]

    return {
        "checks": checks,
        "failed_checks": failed_checks,
        "status": "PASS" if not failed_checks else "FAIL",
    }


def validate_warranty_references(
    records: list[dict[str, Any]],
    support_ticket_ids: set[str],
    reference_sets: dict[str, set[str]],
) -> dict[str, Any]:
    checks = {
        "ticket_id": validate_reference_field(
            records,
            "ticket_id",
            support_ticket_ids,
            required=True,
        ),
        "order_id": validate_reference_field(
            records,
            "linked_entities.order_id",
            reference_sets["order_ids"],
            required=True,
        ),
        "product_id": validate_reference_field(
            records,
            "linked_entities.product_id",
            reference_sets["product_ids"],
            required=True,
        ),
        "seller_id": validate_reference_field(
            records,
            "linked_entities.seller_id",
            reference_sets["seller_ids"],
            required=True,
        ),
        "category_id": validate_reference_field(
            records,
            "linked_entities.category_id",
            reference_sets["category_ids"],
            required=True,
        ),
    }

    failed_checks = [
        check_name
        for check_name, result in checks.items()
        if result["status"] != "PASS"
    ]

    return {
        "checks": checks,
        "failed_checks": failed_checks,
        "status": "PASS" if not failed_checks else "FAIL",
    }


def validate_category_document_references(
    records: list[dict[str, Any]],
    reference_sets: dict[str, set[str]],
    category_required: bool,
) -> dict[str, Any]:
    checks = {
        "category_id": validate_reference_field(
            records,
            "linked_entities.category_id",
            reference_sets["category_ids"],
            required=category_required,
        )
    }

    failed_checks = [
        check_name
        for check_name, result in checks.items()
        if result["status"] != "PASS"
    ]

    return {
        "checks": checks,
        "failed_checks": failed_checks,
        "status": "PASS" if not failed_checks else "FAIL",
    }


def validate_synthetic_data(
    synthetic_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    engine = build_postgres_engine()

    try:
        reference_sets = load_reference_sets(engine)

        artifact_records = {
            artifact_name: load_jsonl(synthetic_dir / filename)
            for artifact_name, filename in EXPECTED_FILES.items()
        }

        file_checks = {}

        for artifact_name, filename in EXPECTED_FILES.items():
            path = synthetic_dir / filename

            file_checks[artifact_name] = {
                "file": str(path),
                "exists": path.exists(),
                "record_count": len(artifact_records[artifact_name]),
                "status": "PASS" if path.exists() else "FAIL",
            }

        structure_checks = {
            artifact_name: validate_basic_structure(artifact_name, records)
            for artifact_name, records in artifact_records.items()
        }

        support_ticket_ids = {
            record["ticket_id"]
            for record in artifact_records["support_tickets"]
            if has_text(record.get("ticket_id"))
        }

        reference_checks = {
            "support_tickets": validate_ticket_references(
                artifact_records["support_tickets"],
                reference_sets,
            ),
            "logistics_incidents": validate_ticket_references(
                artifact_records["logistics_incidents"],
                reference_sets,
            ),
            "customer_emails": validate_email_references(
                artifact_records["customer_emails"],
                support_ticket_ids,
                reference_sets,
            ),
            "warranty_claims": validate_warranty_references(
                artifact_records["warranty_claims"],
                support_ticket_ids,
                reference_sets,
            ),
            "policy_documents": validate_category_document_references(
                artifact_records["policy_documents"],
                reference_sets,
                category_required=False,
            ),
            "troubleshooting_guides": validate_category_document_references(
                artifact_records["troubleshooting_guides"],
                reference_sets,
                category_required=True,
            ),
        }

        failed_file_checks = [
            name for name, result in file_checks.items()
            if result["status"] != "PASS"
        ]

        failed_structure_checks = [
            name for name, result in structure_checks.items()
            if result["status"] != "PASS"
        ]

        failed_reference_checks = [
            name for name, result in reference_checks.items()
            if result["status"] != "PASS"
        ]

        report = {
            "synthetic_dir": str(synthetic_dir),
            "summary": {
                "overall_status": (
                    "PASS"
                    if not failed_file_checks
                    and not failed_structure_checks
                    and not failed_reference_checks
                    else "FAIL"
                ),
                "artifact_type_count": len(EXPECTED_FILES),
                "total_records": sum(len(records) for records in artifact_records.values()),
                "failed_file_checks": failed_file_checks,
                "failed_structure_checks": failed_structure_checks,
                "failed_reference_checks": failed_reference_checks,
            },
            "file_checks": file_checks,
            "structure_checks": structure_checks,
            "reference_checks": reference_checks,
            "reference_set_counts": {
                name: len(values)
                for name, values in reference_sets.items()
            },
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2)

        return report

    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate synthetic enterprise JSONL artifacts."
    )

    parser.add_argument(
        "--synthetic-dir",
        type=Path,
        default=Path("data/synthetic"),
        help="Directory containing generated synthetic JSONL files.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/synthetic_validation_report.json"),
        help="Path to save validation report.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_synthetic_data(
        synthetic_dir=args.synthetic_dir,
        output_path=args.output,
    )

    print("\nSynthetic Data Validation Completed")
    print("-----------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Artifact types checked: {report['summary']['artifact_type_count']}")
    print(f"Total records checked: {report['summary']['total_records']}")
    print(f"Report saved to: {args.output}")

    print("\nFile checks:")
    for name, result in report["file_checks"].items():
        print(f"{name}: {result['status']}, records={result['record_count']}")

    print("\nStructure checks:")
    for name, result in report["structure_checks"].items():
        print(f"{name}: {result['status']}")

    print("\nReference checks:")
    for name, result in report["reference_checks"].items():
        print(f"{name}: {result['status']}")

    if report["summary"]["failed_file_checks"]:
        print(f"\nFailed file checks: {report['summary']['failed_file_checks']}")

    if report["summary"]["failed_structure_checks"]:
        print(f"\nFailed structure checks: {report['summary']['failed_structure_checks']}")

    if report["summary"]["failed_reference_checks"]:
        print(f"\nFailed reference checks: {report['summary']['failed_reference_checks']}")


if __name__ == "__main__":
    main()