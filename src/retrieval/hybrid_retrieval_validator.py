from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hybrid_retriever import run_hybrid_retrieval


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


def validate_test_case(
    test_case: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    case_output_path = output_dir / f"{test_case['name']}.json"

    report = run_hybrid_retrieval(
        query=test_case["query"],
        output_path=case_output_path,
        sql_limit=10,
        graph_limit=10,
        vector_limit=5,
    )

    sql_result = report["retrieval_results"]["sql"]
    graph_result = report["retrieval_results"]["graph"]
    vector_result = report["retrieval_results"]["vector"]

    actual_sql_intent = sql_result["intent"]
    actual_graph_intent = graph_result["intent"]
    actual_vector_groups = vector_result["artifact_groups"]

    failed_checks = []

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

    if sql_result["record_count"] == 0:
        failed_checks.append(
            {
                "check": "sql_record_count",
                "expected": "> 0",
                "actual": 0,
            }
        )

    if graph_result["record_count"] == 0:
        failed_checks.append(
            {
                "check": "graph_record_count",
                "expected": "> 0",
                "actual": 0,
            }
        )

    if vector_result["record_count"] == 0:
        failed_checks.append(
            {
                "check": "vector_record_count",
                "expected": "> 0",
                "actual": 0,
            }
        )

    return {
        "name": test_case["name"],
        "query": test_case["query"],
        "expected": {
            "sql_intent": test_case["expected_sql_intent"],
            "graph_intent": test_case["expected_graph_intent"],
            "vector_groups": test_case["expected_vector_groups"],
        },
        "actual": {
            "sql_intent": actual_sql_intent,
            "graph_intent": actual_graph_intent,
            "vector_groups": actual_vector_groups,
            "sql_record_count": sql_result["record_count"],
            "graph_record_count": graph_result["record_count"],
            "vector_record_count": vector_result["record_count"],
            "case_output_file": str(case_output_path),
        },
        "failed_checks": failed_checks,
        "status": "PASS" if not failed_checks else "FAIL",
    }


def validate_hybrid_retrieval(
    output_path: Path,
    case_output_dir: Path,
) -> dict[str, Any]:
    case_output_dir.mkdir(parents=True, exist_ok=True)

    case_results = []

    for test_case in TEST_CASES:
        print(f"\nRunning hybrid retrieval test: {test_case['name']}")
        case_result = validate_test_case(
            test_case=test_case,
            output_dir=case_output_dir,
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
        json.dump(report, file, indent=2, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate hybrid retrieval routing and retrieval coverage."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/hybrid_retrieval_validation_report.json"),
        help="Path to save hybrid retrieval validation summary.",
    )

    parser.add_argument(
        "--case-output-dir",
        type=Path,
        default=Path("reports/hybrid_retrieval_cases"),
        help="Directory to save individual hybrid retrieval case reports.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_hybrid_retrieval(
        output_path=args.output,
        case_output_dir=args.case_output_dir,
    )

    print("\nHybrid Retrieval Validation Completed")
    print("-------------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Test cases: {report['summary']['test_case_count']}")
    print(f"Report saved to: {args.output}")

    print("\nCase results:")
    for case in report["test_cases"]:
        print(
            f"{case['name']}: {case['status']} "
            f"(SQL={case['actual']['sql_intent']}, "
            f"Graph={case['actual']['graph_intent']}, "
            f"Vector={case['actual']['vector_groups']})"
        )

    if report["summary"]["failed_cases"]:
        print(f"\nFailed cases: {report['summary']['failed_cases']}")


if __name__ == "__main__":
    main()