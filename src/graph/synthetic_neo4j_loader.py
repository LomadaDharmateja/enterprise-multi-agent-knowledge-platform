from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase


BATCH_SIZE = 1000


SYNTHETIC_FILES = {
    "support_tickets": "support_tickets.jsonl",
    "logistics_incidents": "logistics_incidents.jsonl",
    "customer_emails": "customer_emails.jsonl",
    "warranty_claims": "warranty_claims.jsonl",
    "policy_documents": "policy_documents.jsonl",
    "troubleshooting_guides": "troubleshooting_guides.jsonl",
}


NODE_CYPHER = {
    "support_tickets": """
        UNWIND $rows AS row
        MERGE (n:SupportTicket {ticket_id: row.ticket_id})
        SET n += row.properties;
    """,

    "logistics_incidents": """
        UNWIND $rows AS row
        MERGE (n:LogisticsIncident {incident_id: row.incident_id})
        SET n += row.properties;
    """,

    "customer_emails": """
        UNWIND $rows AS row
        MERGE (n:CustomerEmail {email_id: row.email_id})
        SET n += row.properties;
    """,

    "warranty_claims": """
        UNWIND $rows AS row
        MERGE (n:WarrantyClaim {claim_id: row.claim_id})
        SET n += row.properties;
    """,

    "policy_documents": """
        UNWIND $rows AS row
        MERGE (n:PolicyDocument {document_id: row.document_id})
        SET n += row.properties;
    """,

    "troubleshooting_guides": """
        UNWIND $rows AS row
        MERGE (n:TroubleshootingGuide {guide_id: row.guide_id})
        SET n += row.properties;
    """,
}


RELATIONSHIP_CYPHER = {
    "support_tickets": """
        UNWIND $rows AS row
        MATCH (t:SupportTicket {ticket_id: row.ticket_id})
        OPTIONAL MATCH (o:Order {order_id: row.order_id})
        OPTIONAL MATCH (c:Customer {customer_id: row.customer_id})
        OPTIONAL MATCH (p:Product {product_id: row.product_id})
        OPTIONAL MATCH (s:Seller {seller_id: row.seller_id})
        OPTIONAL MATCH (cat:Category {category_id: row.category_id})
        OPTIONAL MATCH (region:Region {region_id: row.region_id})
        OPTIONAL MATCH (r:Review {review_key: row.review_key})

        FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END |
            MERGE (t)-[:ABOUT_ORDER]->(o)
        )
        FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
            MERGE (t)-[:RAISED_BY]->(c)
        )
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END |
            MERGE (t)-[:MENTIONS_PRODUCT]->(p)
        )
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
            MERGE (t)-[:INVOLVES_SELLER]->(s)
        )
        FOREACH (_ IN CASE WHEN cat IS NULL THEN [] ELSE [1] END |
            MERGE (t)-[:RELATED_TO_CATEGORY]->(cat)
        )
        FOREACH (_ IN CASE WHEN region IS NULL THEN [] ELSE [1] END |
            MERGE (t)-[:RELATED_TO_REGION]->(region)
        )
        FOREACH (_ IN CASE WHEN r IS NULL THEN [] ELSE [1] END |
            MERGE (t)-[:ESCALATES_REVIEW]->(r)
        );
    """,

    "logistics_incidents": """
        UNWIND $rows AS row
        MATCH (i:LogisticsIncident {incident_id: row.incident_id})
        OPTIONAL MATCH (o:Order {order_id: row.order_id})
        OPTIONAL MATCH (s:Seller {seller_id: row.seller_id})
        OPTIONAL MATCH (p:Product {product_id: row.product_id})
        OPTIONAL MATCH (region:Region {region_id: row.region_id})

        FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END |
            MERGE (i)-[:AFFECTS_ORDER]->(o)
        )
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
            MERGE (i)-[:INVOLVES_SELLER]->(s)
        )
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END |
            MERGE (i)-[:IMPACTS_PRODUCT]->(p)
        )
        FOREACH (_ IN CASE WHEN region IS NULL THEN [] ELSE [1] END |
            MERGE (i)-[:OCCURRED_IN_REGION]->(region)
        );
    """,

    "customer_emails": """
        UNWIND $rows AS row
        MATCH (e:CustomerEmail {email_id: row.email_id})
        OPTIONAL MATCH (t:SupportTicket {ticket_id: row.ticket_id})
        OPTIONAL MATCH (o:Order {order_id: row.order_id})
        OPTIONAL MATCH (c:Customer {customer_id: row.customer_id})

        FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END |
            MERGE (e)-[:RELATED_TO_TICKET]->(t)
        )
        FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END |
            MERGE (e)-[:ABOUT_ORDER]->(o)
        )
        FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
            MERGE (e)-[:SENT_BY_CUSTOMER]->(c)
        );
    """,

    "warranty_claims": """
        UNWIND $rows AS row
        MATCH (w:WarrantyClaim {claim_id: row.claim_id})
        OPTIONAL MATCH (t:SupportTicket {ticket_id: row.ticket_id})
        OPTIONAL MATCH (o:Order {order_id: row.order_id})
        OPTIONAL MATCH (c:Customer {customer_id: row.customer_id})
        OPTIONAL MATCH (p:Product {product_id: row.product_id})
        OPTIONAL MATCH (s:Seller {seller_id: row.seller_id})
        OPTIONAL MATCH (cat:Category {category_id: row.category_id})

        FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END |
            MERGE (w)-[:CLAIM_FOR_TICKET]->(t)
        )
        FOREACH (_ IN CASE WHEN o IS NULL THEN [] ELSE [1] END |
            MERGE (w)-[:ABOUT_ORDER]->(o)
        )
        FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
            MERGE (w)-[:RAISED_BY]->(c)
        )
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END |
            MERGE (w)-[:CLAIMS_PRODUCT]->(p)
        )
        FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
            MERGE (w)-[:INVOLVES_SELLER]->(s)
        )
        FOREACH (_ IN CASE WHEN cat IS NULL THEN [] ELSE [1] END |
            MERGE (w)-[:RELATED_TO_CATEGORY]->(cat)
        );
    """,

    "policy_documents": """
        UNWIND $rows AS row
        MATCH (d:PolicyDocument {document_id: row.document_id})
        OPTIONAL MATCH (cat:Category {category_id: row.category_id})

        FOREACH (_ IN CASE WHEN cat IS NULL THEN [] ELSE [1] END |
            MERGE (d)-[:APPLIES_TO_CATEGORY]->(cat)
        );
    """,

    "troubleshooting_guides": """
        UNWIND $rows AS row
        MATCH (g:TroubleshootingGuide {guide_id: row.guide_id})
        OPTIONAL MATCH (cat:Category {category_id: row.category_id})

        FOREACH (_ IN CASE WHEN cat IS NULL THEN [] ELSE [1] END |
            MERGE (g)-[:GUIDE_FOR_CATEGORY]->(cat)
        );
    """,
}


NODE_COUNT_CYPHER = {
    "SupportTicket": "MATCH (n:SupportTicket) RETURN count(n) AS count;",
    "LogisticsIncident": "MATCH (n:LogisticsIncident) RETURN count(n) AS count;",
    "CustomerEmail": "MATCH (n:CustomerEmail) RETURN count(n) AS count;",
    "WarrantyClaim": "MATCH (n:WarrantyClaim) RETURN count(n) AS count;",
    "PolicyDocument": "MATCH (n:PolicyDocument) RETURN count(n) AS count;",
    "TroubleshootingGuide": "MATCH (n:TroubleshootingGuide) RETURN count(n) AS count;",
}


RELATIONSHIP_COUNT_CYPHER = {
    "ABOUT_ORDER": "MATCH ()-[r:ABOUT_ORDER]->() RETURN count(r) AS count;",
    "RAISED_BY": "MATCH ()-[r:RAISED_BY]->() RETURN count(r) AS count;",
    "MENTIONS_PRODUCT": "MATCH ()-[r:MENTIONS_PRODUCT]->() RETURN count(r) AS count;",
    "INVOLVES_SELLER": "MATCH ()-[r:INVOLVES_SELLER]->() RETURN count(r) AS count;",
    "RELATED_TO_CATEGORY": "MATCH ()-[r:RELATED_TO_CATEGORY]->() RETURN count(r) AS count;",
    "RELATED_TO_REGION": "MATCH ()-[r:RELATED_TO_REGION]->() RETURN count(r) AS count;",
    "ESCALATES_REVIEW": "MATCH ()-[r:ESCALATES_REVIEW]->() RETURN count(r) AS count;",
    "AFFECTS_ORDER": "MATCH ()-[r:AFFECTS_ORDER]->() RETURN count(r) AS count;",
    "IMPACTS_PRODUCT": "MATCH ()-[r:IMPACTS_PRODUCT]->() RETURN count(r) AS count;",
    "OCCURRED_IN_REGION": "MATCH ()-[r:OCCURRED_IN_REGION]->() RETURN count(r) AS count;",
    "RELATED_TO_TICKET": "MATCH ()-[r:RELATED_TO_TICKET]->() RETURN count(r) AS count;",
    "SENT_BY_CUSTOMER": "MATCH ()-[r:SENT_BY_CUSTOMER]->() RETURN count(r) AS count;",
    "CLAIM_FOR_TICKET": "MATCH ()-[r:CLAIM_FOR_TICKET]->() RETURN count(r) AS count;",
    "CLAIMS_PRODUCT": "MATCH ()-[r:CLAIMS_PRODUCT]->() RETURN count(r) AS count;",
    "APPLIES_TO_CATEGORY": "MATCH ()-[r:APPLIES_TO_CATEGORY]->() RETURN count(r) AS count;",
    "GUIDE_FOR_CATEGORY": "MATCH ()-[r:GUIDE_FOR_CATEGORY]->() RETURN count(r) AS count;",
}


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if line:
                records.append(json.loads(line))

    return records


def has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def sanitize_value(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, float) and pd.isna(value):
        return None

    if pd.isna(value):
        return None

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)

    return value


def flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    linked_entities = record.get("linked_entities") or {}
    metadata = record.get("metadata") or {}

    flattened = {}

    for key, value in record.items():
        if key in {"linked_entities", "metadata"}:
            continue

        flattened[key] = sanitize_value(value)

    for key, value in linked_entities.items():
        flattened[key] = sanitize_value(value)

    flattened["linked_entities_json"] = json.dumps(
        linked_entities,
        ensure_ascii=False,
        default=str,
    )

    flattened["metadata_json"] = json.dumps(
        metadata,
        ensure_ascii=False,
        default=str,
    )

    review_id = linked_entities.get("review_id")
    order_id = linked_entities.get("order_id")

    flattened["review_key"] = (
        f"{review_id}::{order_id}"
        if has_value(review_id) and has_value(order_id)
        else None
    )

    return {
        key: value
        for key, value in flattened.items()
        if value is not None
    }


def prepare_rows(records: list[dict[str, Any]], id_field: str) -> list[dict[str, Any]]:
    rows = []

    for record in records:
        properties = flatten_record(record)
        artifact_id = properties[id_field]

        rows.append(
            {
                id_field: artifact_id,
                "properties": properties,
                "ticket_id": properties.get("ticket_id"),
                "incident_id": properties.get("incident_id"),
                "email_id": properties.get("email_id"),
                "claim_id": properties.get("claim_id"),
                "document_id": properties.get("document_id"),
                "guide_id": properties.get("guide_id"),
                "customer_id": properties.get("customer_id"),
                "order_id": properties.get("order_id"),
                "product_id": properties.get("product_id"),
                "seller_id": properties.get("seller_id"),
                "category_id": properties.get("category_id"),
                "region_id": properties.get("region_id"),
                "review_key": properties.get("review_key"),
            }
        )

    return rows


def execute_batches(
    driver: Driver,
    cypher: str,
    rows: list[dict[str, Any]],
    batch_size: int = BATCH_SIZE,
) -> int:
    written = 0

    with driver.session(database="neo4j") as session:
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            session.execute_write(
                lambda tx, batch_rows: tx.run(cypher, rows=batch_rows).consume(),
                batch,
            )
            written += len(batch)

    return written


def delete_existing_synthetic_graph(driver: Driver) -> None:
    cypher = """
    MATCH (n)
    WHERE n:SupportTicket
       OR n:LogisticsIncident
       OR n:CustomerEmail
       OR n:WarrantyClaim
       OR n:PolicyDocument
       OR n:TroubleshootingGuide
    DETACH DELETE n;
    """

    with driver.session(database="neo4j") as session:
        session.execute_write(lambda tx: tx.run(cypher).consume())


def scalar_query(driver: Driver, cypher: str) -> int:
    with driver.session(database="neo4j") as session:
        result = session.run(cypher)
        record = result.single()

        if record is None:
            return 0

        return int(record["count"])


def load_synthetic_graph(
    synthetic_dir: Path,
    output_path: Path,
    reset: bool,
) -> dict[str, Any]:
    driver = build_neo4j_driver()

    id_fields = {
        "support_tickets": "ticket_id",
        "logistics_incidents": "incident_id",
        "customer_emails": "email_id",
        "warranty_claims": "claim_id",
        "policy_documents": "document_id",
        "troubleshooting_guides": "guide_id",
    }

    try:
        if reset:
            print("Clearing existing synthetic graph nodes...")
            delete_existing_synthetic_graph(driver)

        artifact_results = {}

        for artifact_name, file_name in SYNTHETIC_FILES.items():
            print(f"Loading synthetic nodes: {artifact_name}")

            records = load_jsonl(synthetic_dir / file_name)
            rows = prepare_rows(records, id_fields[artifact_name])

            node_count = execute_batches(
                driver=driver,
                cypher=NODE_CYPHER[artifact_name],
                rows=rows,
            )

            print(f"Creating relationships for: {artifact_name}")

            relationship_source_rows = execute_batches(
                driver=driver,
                cypher=RELATIONSHIP_CYPHER[artifact_name],
                rows=rows,
            )

            artifact_results[artifact_name] = {
                "source_records": len(records),
                "nodes_written": node_count,
                "relationship_source_rows": relationship_source_rows,
                "status": "LOADED",
            }

        node_counts = {
            label: scalar_query(driver, cypher)
            for label, cypher in NODE_COUNT_CYPHER.items()
        }

        relationship_counts = {
            rel_type: scalar_query(driver, cypher)
            for rel_type, cypher in RELATIONSHIP_COUNT_CYPHER.items()
        }

        failed_node_counts = []

        expected_node_counts = {
            "SupportTicket": artifact_results["support_tickets"]["source_records"],
            "LogisticsIncident": artifact_results["logistics_incidents"]["source_records"],
            "CustomerEmail": artifact_results["customer_emails"]["source_records"],
            "WarrantyClaim": artifact_results["warranty_claims"]["source_records"],
            "PolicyDocument": artifact_results["policy_documents"]["source_records"],
            "TroubleshootingGuide": artifact_results["troubleshooting_guides"]["source_records"],
        }

        for label, expected in expected_node_counts.items():
            actual = node_counts[label]

            if expected != actual:
                failed_node_counts.append(
                    {
                        "label": label,
                        "expected": expected,
                        "actual": actual,
                    }
                )

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "synthetic_dir": str(synthetic_dir),
            "reset_before_load": reset,
            "summary": {
                "overall_status": "PASS" if not failed_node_counts else "FAIL",
                "artifact_type_count": len(SYNTHETIC_FILES),
                "failed_node_counts": failed_node_counts,
            },
            "artifacts": artifact_results,
            "node_counts": node_counts,
            "relationship_counts": relationship_counts,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, default=str)

        return report

    finally:
        driver.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load synthetic enterprise artifacts into Neo4j."
    )

    parser.add_argument(
        "--synthetic-dir",
        type=Path,
        default=Path("data/synthetic"),
        help="Directory containing synthetic JSONL files.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/synthetic_neo4j_load_report.json"),
        help="Path to save synthetic Neo4j load report.",
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing synthetic graph nodes before loading.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = load_synthetic_graph(
        synthetic_dir=args.synthetic_dir,
        output_path=args.output,
        reset=args.reset,
    )

    print("\nSynthetic Neo4j Load Completed")
    print("------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Artifact types loaded: {report['summary']['artifact_type_count']}")
    print(f"Report saved to: {args.output}")

    print("\nSynthetic node counts:")
    for label, count in report["node_counts"].items():
        print(f"{label}: {count}")

    print("\nSynthetic relationship counts:")
    for rel_type, count in report["relationship_counts"].items():
        print(f"{rel_type}: {count}")

    if report["summary"]["failed_node_counts"]:
        print(f"\nFailed node counts: {report['summary']['failed_node_counts']}")


if __name__ == "__main__":
    main()