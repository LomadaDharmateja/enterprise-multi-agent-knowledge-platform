from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_workflow import run_agentic_workflow


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


REQUIRED_OUTPUT_FILES = [
    "raw_retrieval",
    "retrieval_context",
    "answer_json",
    "answer_markdown",
    "prompt",
    "evaluation_json",
    "workflow_report",
]


def validate_case(
    test_case: dict[str, Any],
    case_output_root: Path,
) -> dict[str, Any]:
    case_output_dir = case_output_root / test_case["name"]

    result = run_agentic_workflow(
        query=test_case["query"],
        output_dir=case_output_dir,
    )

    failed_checks = []

    if result["overall_status"] != "PASS":
        failed_checks.append(
            {
                "check": "workflow_status",
                "expected": "PASS",
                "actual": result["overall_status"],
            }
        )

    actual_sql_intent = result["source_summary"]["sql"]["intent"]
    actual_graph_intent = result["source_summary"]["graph"]["intent"]
    actual_vector_groups = result["source_summary"]["vector"]["artifact_groups"]

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

    if result["answer_provider"] != "gemini":
        failed_checks.append(
            {
                "check": "answer_provider",
                "expected": "gemini",
                "actual": result["answer_provider"],
            }
        )

    if not str(result["answer_model"]).startswith("gemini"):
        failed_checks.append(
            {
                "check": "answer_model",
                "expected": "gemini model",
                "actual": result["answer_model"],
            }
        )

    if result["answer_length_chars"] < 1000:
        failed_checks.append(
            {
                "check": "answer_length_chars",
                "expected": ">= 1000",
                "actual": result["answer_length_chars"],
            }
        )
    
    evaluation_summary = result.get("evaluation_summary", {})

    if not evaluation_summary:
        failed_checks.append(
            {
                "check": "evaluation_summary_present",
                "expected": "evaluation summary in workflow result",
                "actual": "missing",
            }
        )
    else:
        if evaluation_summary.get("overall_status") != "PASS":
            failed_checks.append(
                {
                    "check": "evaluation_status",
                    "expected": "PASS",
                    "actual": evaluation_summary.get("overall_status"),
                }
            )

        if int(evaluation_summary.get("grounding_score") or 0) < 3:
            failed_checks.append(
                {
                    "check": "grounding_score",
                    "expected": ">= 3",
                    "actual": evaluation_summary.get("grounding_score"),
                }
            )

        if int(evaluation_summary.get("completeness_score") or 0) < 3:
            failed_checks.append(
                {
                    "check": "completeness_score",
                    "expected": ">= 3",
                    "actual": evaluation_summary.get("completeness_score"),
                }
            )

        if int(evaluation_summary.get("business_readiness_score") or 0) < 3:
            failed_checks.append(
                {
                    "check": "business_readiness_score",
                    "expected": ">= 3",
                    "actual": evaluation_summary.get("business_readiness_score"),
                }
            )

        if evaluation_summary.get("unsupported_claims"):
            failed_checks.append(
                {
                    "check": "unsupported_claims",
                    "expected": 0,
                    "actual": len(evaluation_summary.get("unsupported_claims", [])),
                }
            )    

    if test_case["query"] not in result["answer_preview"]:
        failed_checks.append(
            {
                "check": "query_in_answer_preview",
                "expected": test_case["query"],
                "actual": "missing",
            }
        )

    for file_key in REQUIRED_OUTPUT_FILES:
        output_file = result["output_files"].get(file_key)

        if not output_file:
            failed_checks.append(
                {
                    "check": "output_file_registered",
                    "expected": file_key,
                    "actual": "missing",
                }
            )
            continue

        if not Path(output_file).exists():
            failed_checks.append(
                {
                    "check": "output_file_exists",
                    "expected": output_file,
                    "actual": "missing",
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
            "answer_provider": result["answer_provider"],
            "answer_model": result["answer_model"],
            "answer_length_chars": result["answer_length_chars"],
            "output_dir": str(case_output_dir),
            "evaluation_status": result.get("evaluation_summary", {}).get("overall_status"),
            "grounding_score": result.get("evaluation_summary", {}).get("grounding_score"),
            "completeness_score": result.get("evaluation_summary", {}).get("completeness_score"),
            "business_readiness_score": result.get("evaluation_summary", {}).get("business_readiness_score"),
        },
    }


def validate_agentic_workflow(
    output_path: Path,
    case_output_root: Path,
) -> dict[str, Any]:
    case_output_root.mkdir(parents=True, exist_ok=True)

    case_results = []

    for test_case in TEST_CASES:
        print(f"\nRunning agentic workflow test: {test_case['name']}")
        case_result = validate_case(
            test_case=test_case,
            case_output_root=case_output_root,
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
        json.dump(report, file, indent=2, ensure_ascii=False, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the enterprise agentic workflow across business test cases."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/agentic_workflow_validation_report.json"),
        help="Path to save agentic workflow validation report.",
    )

    parser.add_argument(
        "--case-output-root",
        type=Path,
        default=Path("reports/agentic_workflow_cases"),
        help="Directory to save individual workflow case outputs.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_agentic_workflow(
        output_path=args.output,
        case_output_root=args.case_output_root,
    )

    print("\nAgentic Workflow Validation Completed")
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
            f"Vector={case['actual']['vector_groups']}, "
            f"Answer chars={case['actual']['answer_length_chars']})"
            f"Answer chars={case['actual']['answer_length_chars']}, "
            f"Eval={case['actual'].get('evaluation_status')}, "
            f"Grounding={case['actual'].get('grounding_score')})"
        )

    if report["summary"]["failed_cases"]:
        print(f"\nFailed cases: {report['summary']['failed_cases']}")


if __name__ == "__main__":
    main()