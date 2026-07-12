from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase


NODE_LABELS = [
    "SupportTicket",
    "LogisticsIncident",
    "CustomerEmail",
    "WarrantyClaim",
    "PolicyDocument",
    "TroubleshootingGuide",
]


RELATIONSHIP_TYPES = [
    "ABOUT_ORDER",
    "RAISED_BY",
    "MENTIONS_PRODUCT",
    "INVOLVES_SELLER",
    "RELATED_TO_CATEGORY",
    "RELATED_TO_REGION",
    "ESCALATES_REVIEW",
    "AFFECTS_ORDER",
    "IMPACTS_PRODUCT",
    "OCCURRED_IN_REGION",
    "RELATED_TO_TICKET",
    "SENT_BY_CUSTOMER",
    "CLAIM_FOR_TICKET",
    "CLAIMS_PRODUCT",
    "APPLIES_TO_CATEGORY",
    "GUIDE_FOR_CATEGORY",
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
    return {
        label: scalar_query(driver, f"MATCH (n:{label}) RETURN count(n) AS count;")
        for label in NODE_LABELS
    }


def get_relationship_counts(driver: Driver) -> dict[str, int]:
    return {
        rel_type: scalar_query(driver, f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS count;")
        for rel_type in RELATIONSHIP_TYPES
    }


def get_orphan_checks(driver: Driver) -> dict[str, int]:
    checks = {
        "support_tickets_without_order": """
            MATCH (t:SupportTicket)
            WHERE NOT EXISTS {
                MATCH (t)-[:ABOUT_ORDER]->(:Order)
            }
            RETURN count(t) AS count;
        """,
        "support_tickets_without_customer": """
            MATCH (t:SupportTicket)
            WHERE NOT EXISTS {
                MATCH (t)-[:RAISED_BY]->(:Customer)
            }
            RETURN count(t) AS count;
        """,
        "support_tickets_without_product": """
            MATCH (t:SupportTicket)
            WHERE NOT EXISTS {
                MATCH (t)-[:MENTIONS_PRODUCT]->(:Product)
            }
            RETURN count(t) AS count;
        """,
        "support_tickets_without_seller": """
            MATCH (t:SupportTicket)
            WHERE NOT EXISTS {
                MATCH (t)-[:INVOLVES_SELLER]->(:Seller)
            }
            RETURN count(t) AS count;
        """,
        "support_tickets_without_category": """
            MATCH (t:SupportTicket)
            WHERE NOT EXISTS {
                MATCH (t)-[:RELATED_TO_CATEGORY]->(:Category)
            }
            RETURN count(t) AS count;
        """,
        "support_tickets_without_region": """
            MATCH (t:SupportTicket)
            WHERE NOT EXISTS {
                MATCH (t)-[:RELATED_TO_REGION]->(:Region)
            }
            RETURN count(t) AS count;
        """,
        "logistics_incidents_without_order": """
            MATCH (i:LogisticsIncident)
            WHERE NOT EXISTS {
                MATCH (i)-[:AFFECTS_ORDER]->(:Order)
            }
            RETURN count(i) AS count;
        """,
        "logistics_incidents_without_seller": """
            MATCH (i:LogisticsIncident)
            WHERE NOT EXISTS {
                MATCH (i)-[:INVOLVES_SELLER]->(:Seller)
            }
            RETURN count(i) AS count;
        """,
        "customer_emails_without_ticket": """
            MATCH (e:CustomerEmail)
            WHERE NOT EXISTS {
                MATCH (e)-[:RELATED_TO_TICKET]->(:SupportTicket)
            }
            RETURN count(e) AS count;
        """,
        "customer_emails_without_customer": """
            MATCH (e:CustomerEmail)
            WHERE NOT EXISTS {
                MATCH (e)-[:SENT_BY_CUSTOMER]->(:Customer)
            }
            RETURN count(e) AS count;
        """,
        "warranty_claims_without_ticket": """
            MATCH (w:WarrantyClaim)
            WHERE NOT EXISTS {
                MATCH (w)-[:CLAIM_FOR_TICKET]->(:SupportTicket)
            }
            RETURN count(w) AS count;
        """,
        "warranty_claims_without_product": """
            MATCH (w:WarrantyClaim)
            WHERE NOT EXISTS {
                MATCH (w)-[:CLAIMS_PRODUCT]->(:Product)
            }
            RETURN count(w) AS count;
        """,
        "warranty_claims_without_seller": """
            MATCH (w:WarrantyClaim)
            WHERE NOT EXISTS {
                MATCH (w)-[:INVOLVES_SELLER]->(:Seller)
            }
            RETURN count(w) AS count;
        """,
        "category_policy_documents_without_category": """
            MATCH (p:PolicyDocument)
            WHERE p.policy_topic = 'category_support_policy'
              AND NOT EXISTS {
                  MATCH (p)-[:APPLIES_TO_CATEGORY]->(:Category)
              }
            RETURN count(p) AS count;
        """,
        "troubleshooting_guides_without_category": """
            MATCH (g:TroubleshootingGuide)
            WHERE NOT EXISTS {
                MATCH (g)-[:GUIDE_FOR_CATEGORY]->(:Category)
            }
            RETURN count(g) AS count;
        """,
    }

    return {
        check_name: scalar_query(driver, cypher)
        for check_name, cypher in checks.items()
    }


def get_business_path_checks(driver: Driver) -> dict[str, int]:
    checks = {
        "customer_ticket_order_product_paths": """
            MATCH (c:Customer)<-[:RAISED_BY]-(t:SupportTicket)-[:ABOUT_ORDER]->(o:Order)
            MATCH (t)-[:MENTIONS_PRODUCT]->(p:Product)
            RETURN count(t) AS count;
        """,
        "ticket_email_customer_paths": """
            MATCH (c:Customer)<-[:SENT_BY_CUSTOMER]-(e:CustomerEmail)-[:RELATED_TO_TICKET]->(t:SupportTicket)
            RETURN count(e) AS count;
        """,
        "ticket_review_escalation_paths": """
            MATCH (t:SupportTicket)-[:ESCALATES_REVIEW]->(r:Review)<-[:HAS_REVIEW]-(o:Order)
            RETURN count(t) AS count;
        """,
        "seller_ticket_product_paths": """
            MATCH (s:Seller)<-[:INVOLVES_SELLER]-(t:SupportTicket)-[:MENTIONS_PRODUCT]->(p:Product)
            RETURN count(t) AS count;
        """,
        "logistics_region_order_paths": """
            MATCH (i:LogisticsIncident)-[:OCCURRED_IN_REGION]->(r:Region)
            MATCH (i)-[:AFFECTS_ORDER]->(o:Order)
            RETURN count(i) AS count;
        """,
        "warranty_ticket_product_seller_paths": """
            MATCH (w:WarrantyClaim)-[:CLAIM_FOR_TICKET]->(t:SupportTicket)
            MATCH (w)-[:CLAIMS_PRODUCT]->(p:Product)
            MATCH (w)-[:INVOLVES_SELLER]->(s:Seller)
            RETURN count(w) AS count;
        """,
        "category_policy_guide_paths": """
            MATCH (p:PolicyDocument)-[:APPLIES_TO_CATEGORY]->(c:Category)<-[:GUIDE_FOR_CATEGORY]-(g:TroubleshootingGuide)
            RETURN count(c) AS count;
        """,
        "negative_review_ticket_product_paths": """
            MATCH (t:SupportTicket)-[:ESCALATES_REVIEW]->(r:Review)
            MATCH (t)-[:MENTIONS_PRODUCT]->(p:Product)
            WHERE r.review_score <= 2
            RETURN count(t) AS count;
        """,
    }

    return {
        check_name: scalar_query(driver, cypher)
        for check_name, cypher in checks.items()
    }


def get_sample_business_queries(driver: Driver) -> dict[str, list[dict[str, Any]]]:
    return {
        "top_ticket_issue_types": run_query(
            driver,
            """
            MATCH (t:SupportTicket)
            RETURN
                t.issue_type AS issue_type,
                count(t) AS ticket_count
            ORDER BY ticket_count DESC
            LIMIT 10;
            """,
        ),
        "top_sellers_by_ticket_volume": run_query(
            driver,
            """
            MATCH (t:SupportTicket)-[:INVOLVES_SELLER]->(s:Seller)
            RETURN
                s.seller_id AS seller_id,
                s.seller_state AS seller_state,
                count(t) AS ticket_count
            ORDER BY ticket_count DESC
            LIMIT 10;
            """,
        ),
        "top_categories_by_warranty_claims": run_query(
            driver,
            """
            MATCH (w:WarrantyClaim)-[:RELATED_TO_CATEGORY]->(c:Category)
            RETURN
                c.product_category_name_english AS category,
                count(w) AS warranty_claim_count
            ORDER BY warranty_claim_count DESC
            LIMIT 10;
            """,
        ),
        "regions_by_logistics_incidents": run_query(
            driver,
            """
            MATCH (i:LogisticsIncident)-[:OCCURRED_IN_REGION]->(r:Region)
            RETURN
                r.state AS state,
                r.city AS city,
                count(i) AS incident_count
            ORDER BY incident_count DESC
            LIMIT 10;
            """,
        ),
        "policy_documents_by_category": run_query(
            driver,
            """
            MATCH (p:PolicyDocument)-[:APPLIES_TO_CATEGORY]->(c:Category)
            RETURN
                c.product_category_name_english AS category,
                count(p) AS policy_document_count
            ORDER BY policy_document_count DESC
            LIMIT 10;
            """,
        ),
    }


def validate_synthetic_graph(output_path: Path) -> dict[str, Any]:
    driver = build_neo4j_driver()

    try:
        print("Checking synthetic node counts...")
        node_counts = get_node_counts(driver)

        print("Checking synthetic relationship counts...")
        relationship_counts = get_relationship_counts(driver)

        print("Checking synthetic orphan relationships...")
        orphan_checks = get_orphan_checks(driver)

        print("Checking synthetic business paths...")
        business_path_checks = get_business_path_checks(driver)

        print("Running sample business queries...")
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
        description="Validate synthetic Neo4j graph readiness."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/synthetic_graph_validation_report.json"),
        help="Path to save synthetic graph validation report.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_synthetic_graph(output_path=args.output)

    print("\nSynthetic Graph Validation Completed")
    print("------------------------------------")
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