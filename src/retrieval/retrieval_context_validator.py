from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from retrieval_context_builder import build_retrieval_context


TEST_CASES = [
    {
        "name": "seller_complaint_warranty_policy",
        "query": "Find sellers with negative customer complaints, warranty issues, and relevant support policies",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": "warranty_product_seller_paths",
        "expected_vector_groups": [
            "support_tickets",
            "warranty_claims",
            "policy_documents",
        ],
    },
    {
        "name": "late_delivery_logistics_region_guidance",
        "query": "Investigate late delivery logistics incidents by customer region and find troubleshooting guidance",
        "expected_sql_intent": "order_summary",
        "expected_graph_intent": "logistics_region_paths",
        "expected_vector_groups": [
            "logistics_incidents",
            "troubleshooting_guides",
        ],
    },
    {
        "name": "payment_refund_policy",
        "query": "Find payment questions, refund policies, and customer support cases",
        "expected_sql_intent": "payment_summary",
        "expected_graph_intent": "customer_ticket_order_product_paths",
        "expected_vector_groups": [
            "support_tickets",
            "policy_documents",
        ],
    },
    {
        "name": "product_quality_troubleshooting",
        "query": "Investigate product quality complaints and find troubleshooting procedures",
        "expected_sql_intent": "review_intelligence",
        "expected_graph_intent": "customer_ticket_order_product_paths",
        "expected_vector_groups": [
            "support_tickets",
            "troubleshooting_guides",
        ],
    },
    {
        "name": "seller_negative_ticket_paths",
        "query": "Show seller ticket paths for negative complaints and product issues",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": "seller_ticket_product_paths",
        "expected_vector_groups": [
            "support_tickets",
        ],
    },
]


REQUIRED_CONTEXT_KEYS = [
    "generated_at",
    "business_question",
    "summary",
    "source_summary",
    "sql_evidence",
    "graph_evidence",
    "document_evidence",
    "entity_ids",
    "recommended_next_actions",
    "answer_context_text",
]


def validate_context_case(
    test_case: dict[str, Any],
    case_output_dir: Path,
) -> dict[str, Any]:
    raw_output_path = case_output_dir / f"{test_case['name']}_raw.json"
    context_output_path = case_output_dir / f"{test_case['name']}_context.json"

    context = build_retrieval_context(
        query=test_case["query"],
        raw_report_path=raw_output_path,
        context_output_path=context_output_path,
        sql_limit=10,
        graph_limit=10,
        vector_limit=5,
        max_records_per_section=5,
    )

    failed_checks = []

    for key in REQUIRED_CONTEXT_KEYS:
        if key not in context:
            failed_checks.append(
                {
                    "check": "required_context_key",
                    "expected": key,
                    "actual": "missing",
                }
            )

    actual_sql_intent = context["source_summary"]["sql"]["intent"]
    actual_graph_intent = context["source_summary"]["graph"]["intent"]
    actual_vector_groups = context["source_summary"]["vector"]["artifact_groups"]

    if actual_sql_intent != test_case["expected_sql_intent"]:
        failed_checks.append(
            {
                "check": "sql_intent",
                "expected": test_case["expected_sql_intent"],
                "actual": actual_sql_intent,
            }
        )

    if actual_graph_intent != test_case["expected_graph_intent"]:
        failed_checks.append(
            {
                "check": "graph_intent",
                "expected": test_case["expected_graph_intent"],
                "actual": actual_graph_intent,
            }
        )

    for expected_group in test_case["expected_vector_groups"]:
        if expected_group not in actual_vector_groups:
            failed_checks.append(
                {
                    "check": "vector_group_missing",
                    "expected": expected_group,
                    "actual": actual_vector_groups,
                }
            )

    if context["sql_evidence"]["records_in_context"] == 0:
        failed_checks.append(
            {
                "check": "sql_evidence_non_empty",
                "expected": "> 0",
                "actual": 0,
            }
        )

    if context["graph_evidence"]["records_in_context"] == 0:
        failed_checks.append(
            {
                "check": "graph_evidence_non_empty",
                "expected": "> 0",
                "actual": 0,
            }
        )

    document_group_counts = {
        group: len(records)
        for group, records in context["document_evidence"]["results_by_artifact_group"].items()
    }

    if sum(document_group_counts.values()) == 0:
        failed_checks.append(
            {
                "check": "document_evidence_non_empty",
                "expected": "> 0",
                "actual": 0,
            }
        )

    if not context["entity_ids"]:
        failed_checks.append(
            {
                "check": "entity_ids_non_empty",
                "expected": "> 0 entity groups",
                "actual": 0,
            }
        )

    if not context["recommended_next_actions"]:
        failed_checks.append(
            {
                "check": "recommended_actions_non_empty",
                "expected": "> 0 actions",
                "actual": 0,
            }
        )

    if len(context["answer_context_text"].strip()) < 100:
        failed_checks.append(
            {
                "check": "answer_context_text_length",
                "expected": ">= 100 characters",
                "actual": len(context["answer_context_text"].strip()),
            }
        )

    return {
        "name": test_case["name"],
        "query": test_case["query"],
        "status": "PASS" if not failed_checks else "FAIL",
        "failed_checks": failed_checks,
        "actual": {
            "sql_intent": actual_sql_intent,
            "graph_intent": actual_graph_intent,
            "vector_groups": actual_vector_groups,
            "sql_records_in_context": context["sql_evidence"]["records_in_context"],
            "graph_records_in_context": context["graph_evidence"]["records_in_context"],
            "document_group_counts": document_group_counts,
            "entity_group_count": len(context["entity_ids"]),
            "recommended_action_count": len(context["recommended_next_actions"]),
            "context_file": str(context_output_path),
            "raw_file": str(raw_output_path),
        },
    }


def validate_retrieval_contexts(
    output_path: Path,
    case_output_dir: Path,
) -> dict[str, Any]:
    case_output_dir.mkdir(parents=True, exist_ok=True)

    case_results = []

    for test_case in TEST_CASES:
        print(f"\nRunning retrieval context test: {test_case['name']}")
        case_result = validate_context_case(
            test_case=test_case,
            case_output_dir=case_output_dir,
        )
        case_results.append(case_result)

    failed_cases = [
        result["name"]
        for result in case_results
        if result["status"] != "PASS"
    ]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "overall_status": "PASS" if not failed_cases else "FAIL",
            "test_case_count": len(TEST_CASES),
            "failed_cases": failed_cases,
        },
        "test_cases": case_results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, default=str, ensure_ascii=False)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate answer-ready retrieval contexts across business test cases."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/retrieval_context_validation_report.json"),
        help="Path to save retrieval context validation report.",
    )

    parser.add_argument(
        "--case-output-dir",
        type=Path,
        default=Path("reports/retrieval_context_cases"),
        help="Directory to save individual raw and context reports.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_retrieval_contexts(
        output_path=args.output,
        case_output_dir=args.case_output_dir,
    )

    print("\nRetrieval Context Validation Completed")
    print("--------------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Test cases: {report['summary']['test_case_count']}")
    print(f"Report saved to: {args.output}")

    print("\nCase results:")
    for case in report["test_cases"]:
        print(
            f"{case['name']}: {case['status']} "
            f"(SQL={case['actual']['sql_intent']}, "
            f"Graph={case['actual']['graph_intent']}, "
            f"Vector={case['actual']['vector_groups']}, "
            f"Entities={case['actual']['entity_group_count']})"
        )

    if report["summary"]["failed_cases"]:
        print(f"\nFailed cases: {report['summary']['failed_cases']}")


if __name__ == "__main__":
    main()