from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def get_json(url: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url=url,
        method="GET",
        headers={"Accept": "application/json"},
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def validate_root(api_base_url: str) -> dict[str, Any]:
    failed_checks = []

    response = get_json(f"{api_base_url}/")

    if response.get("status") != "running":
        failed_checks.append(
            {
                "check": "root_status",
                "expected": "running",
                "actual": response.get("status"),
            }
        )

    if "query" not in response.get("endpoints", {}):
        failed_checks.append(
            {
                "check": "root_query_endpoint",
                "expected": "query endpoint registered",
                "actual": response.get("endpoints"),
            }
        )

    return {
        "name": "root",
        "status": "PASS" if not failed_checks else "FAIL",
        "failed_checks": failed_checks,
        "response": response,
    }


def validate_health(api_base_url: str) -> dict[str, Any]:
    failed_checks = []

    response = get_json(f"{api_base_url}/health")

    if response.get("status") != "ok":
        failed_checks.append(
            {
                "check": "health_status",
                "expected": "ok",
                "actual": response.get("status"),
            }
        )

    if response.get("service") != "enterprise-agentic-workflow-api":
        failed_checks.append(
            {
                "check": "health_service",
                "expected": "enterprise-agentic-workflow-api",
                "actual": response.get("service"),
            }
        )

    return {
        "name": "health",
        "status": "PASS" if not failed_checks else "FAIL",
        "failed_checks": failed_checks,
        "response": response,
    }


def validate_query_case(api_base_url: str, test_case: dict[str, Any]) -> dict[str, Any]:
    failed_checks = []

    response = post_json(
        url=f"{api_base_url}/query",
        payload={"query": test_case["query"]},
        timeout=300,
    )

    if response.get("overall_status") != "PASS":
        failed_checks.append(
            {
                "check": "overall_status",
                "expected": "PASS",
                "actual": response.get("overall_status"),
            }
        )

    source_summary = response.get("source_summary", {})

    actual_sql_intent = source_summary.get("sql", {}).get("intent")
    actual_graph_intent = source_summary.get("graph", {}).get("intent")
    actual_vector_groups = source_summary.get("vector", {}).get("artifact_groups", [])

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

    if response.get("answer_provider") != "gemini":
        failed_checks.append(
            {
                "check": "answer_provider",
                "expected": "gemini",
                "actual": response.get("answer_provider"),
            }
        )

    if not str(response.get("answer_model")).startswith("gemini"):
        failed_checks.append(
            {
                "check": "answer_model",
                "expected": "gemini model",
                "actual": response.get("answer_model"),
            }
        )

    if int(response.get("answer_length_chars") or 0) < 1000:
        failed_checks.append(
            {
                "check": "answer_length_chars",
                "expected": ">= 1000",
                "actual": response.get("answer_length_chars"),
            }
        )
    
    evaluation_summary = response.get("evaluation_summary", {})

    if not evaluation_summary:
        failed_checks.append(
            {
                "check": "evaluation_summary_present",
                "expected": "evaluation summary",
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

        if evaluation_summary.get("unsupported_claims"):
            failed_checks.append(
                {
                    "check": "unsupported_claims",
                    "expected": 0,
                    "actual": len(evaluation_summary.get("unsupported_claims", [])),
                }
            )


    if "# Grounded Business Answer" not in response.get("answer_preview", ""):
        failed_checks.append(
            {
                "check": "answer_preview",
                "expected": "Grounded Business Answer markdown preview",
                "actual": response.get("answer_preview", "")[:100],
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
            "answer_provider": response.get("answer_provider"),
            "answer_model": response.get("answer_model"),
            "answer_length_chars": response.get("answer_length_chars"),
            "evaluation_status": evaluation_summary.get("overall_status"),
            "grounding_score": evaluation_summary.get("grounding_score"),
        },
    }


def validate_api(api_base_url: str, output_path: Path) -> dict[str, Any]:
    checks = []

    print("Checking API root endpoint...")
    checks.append(validate_root(api_base_url))

    print("Checking API health endpoint...")
    checks.append(validate_health(api_base_url))

    query_case_results = []

    for test_case in TEST_CASES:
        print(f"\nChecking query endpoint: {test_case['name']}")
        query_case_results.append(
            validate_query_case(
                api_base_url=api_base_url,
                test_case=test_case,
            )
        )

    failed_sections = [
        check["name"]
        for check in checks
        if check["status"] != "PASS"
    ]

    failed_query_cases = [
        case["name"]
        for case in query_case_results
        if case["status"] != "PASS"
    ]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_base_url": api_base_url,
        "summary": {
            "overall_status": (
                "PASS"
                if not failed_sections and not failed_query_cases
                else "FAIL"
            ),
            "endpoint_check_count": len(checks),
            "query_case_count": len(query_case_results),
            "failed_sections": failed_sections,
            "failed_query_cases": failed_query_cases,
        },
        "endpoint_checks": checks,
        "query_case_results": query_case_results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate FastAPI backend endpoints for the enterprise AI workflow."
    )

    parser.add_argument(
        "--api-base-url",
        type=str,
        default="http://127.0.0.1:8000",
        help="Base URL of the running FastAPI server.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/api_validation_report.json"),
        help="Path to save API validation report.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        report = validate_api(
            api_base_url=args.api_base_url.rstrip("/"),
            output_path=args.output,
        )
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Could not reach the FastAPI server. Make sure it is running with: "
            "uvicorn src.api.main:app --reload --port 8000"
        ) from exc

    print("\nAPI Validation Completed")
    print("------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Endpoint checks: {report['summary']['endpoint_check_count']}")
    print(f"Query cases: {report['summary']['query_case_count']}")
    print(f"Report saved to: {args.output}")

    print("\nQuery case results:")
    for case in report["query_case_results"]:
        print(
            f"{case['name']}: {case['status']} "
            f"(SQL={case['actual']['sql_intent']}, "
            f"Graph={case['actual']['graph_intent']}, "
            f"Vector={case['actual']['vector_groups']}, "
            f"Answer chars={case['actual']['answer_length_chars']})"
            f"Eval={case['actual'].get('evaluation_status')}, "
            f"Grounding={case['actual'].get('grounding_score')})"
        )

    if report["summary"]["failed_sections"]:
        print(f"\nFailed sections: {report['summary']['failed_sections']}")

    if report["summary"]["failed_query_cases"]:
        print(f"\nFailed query cases: {report['summary']['failed_query_cases']}")


if __name__ == "__main__":
    main()