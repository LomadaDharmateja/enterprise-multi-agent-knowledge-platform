from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sentence_transformers import SentenceTransformer


EXPECTED_ARTIFACT_COUNTS = {
    "support_tickets": 3000,
    "logistics_incidents": 1000,
    "customer_emails": 3000,
    "warranty_claims": 1000,
    "policy_documents": 79,
    "troubleshooting_guides": 73,
}


SEARCH_TESTS = [
    {
        "name": "late_delivery_support_tickets",
        "query": "customer complains that the delivery arrived late and wants support help",
        "artifact_group": "support_tickets",
        "limit": 5,
    },
    {
        "name": "logistics_delay_incidents",
        "query": "carrier delay regional delivery bottleneck logistics incident",
        "artifact_group": "logistics_incidents",
        "limit": 5,
    },
    {
        "name": "customer_complaint_emails",
        "query": "customer email asking support to review an order problem",
        "artifact_group": "customer_emails",
        "limit": 5,
    },
    {
        "name": "warranty_product_claims",
        "query": "warranty claim for damaged product replacement or refund",
        "artifact_group": "warranty_claims",
        "limit": 5,
    },
    {
        "name": "refund_policy_documents",
        "query": "policy for late delivery refund damaged item return seller escalation",
        "artifact_group": "policy_documents",
        "limit": 5,
    },
    {
        "name": "category_troubleshooting_guides",
        "query": "troubleshooting guide to investigate product quality delivery payment warranty issue",
        "artifact_group": "troubleshooting_guides",
        "limit": 5,
    },
]


def load_settings() -> dict[str, Any]:
    load_dotenv()

    return {
        "qdrant_host": os.getenv("QDRANT_HOST", "localhost"),
        "qdrant_port": int(os.getenv("QDRANT_HTTP_PORT", "6333")),
        "collection_name": os.getenv("QDRANT_COLLECTION", "enterprise_knowledge"),
        "embedding_model_name": os.getenv(
            "EMBEDDING_MODEL_NAME",
            "sentence-transformers/all-MiniLM-L6-v2",
        ),
        "embedding_dimension": int(os.getenv("EMBEDDING_DIMENSION", "384")),
    }


def build_qdrant_client(settings: dict[str, Any]) -> QdrantClient:
    return QdrantClient(
        host=settings["qdrant_host"],
        port=settings["qdrant_port"],
    )


def build_artifact_filter(artifact_group: str) -> Filter:
    return Filter(
        must=[
            FieldCondition(
                key="artifact_group",
                match=MatchValue(value=artifact_group),
            )
        ]
    )


def get_vector_size(collection_info: Any) -> int | None:
    vectors_config = collection_info.config.params.vectors

    if hasattr(vectors_config, "size"):
        return int(vectors_config.size)

    if isinstance(vectors_config, dict):
        first_vector_config = next(iter(vectors_config.values()))
        return int(first_vector_config.size)

    return None


def qdrant_search(
    client: QdrantClient,
    collection_name: str,
    query_vector: list[float],
    limit: int,
    query_filter: Filter | None = None,
) -> list[Any]:
    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return list(response.points)

    return client.search(
        collection_name=collection_name,
        query_vector=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )


def get_collection_checks(
    client: QdrantClient,
    collection_name: str,
    expected_dimension: int,
) -> dict[str, Any]:
    exists = client.collection_exists(collection_name)

    if not exists:
        return {
            "collection_exists": False,
            "points_count": 0,
            "vector_size": None,
            "expected_vector_size": expected_dimension,
            "status": "FAIL",
        }

    collection_info = client.get_collection(collection_name)
    vector_size = get_vector_size(collection_info)
    points_count = int(collection_info.points_count or 0)

    status = (
        "PASS"
        if exists
        and vector_size == expected_dimension
        and points_count > 0
        else "FAIL"
    )

    return {
        "collection_exists": exists,
        "points_count": points_count,
        "vector_size": vector_size,
        "expected_vector_size": expected_dimension,
        "status": status,
    }


def get_artifact_count_checks(
    client: QdrantClient,
    collection_name: str,
) -> dict[str, Any]:
    checks = {}
    failed_artifacts = []

    for artifact_group, expected_count in EXPECTED_ARTIFACT_COUNTS.items():
        result = client.count(
            collection_name=collection_name,
            count_filter=build_artifact_filter(artifact_group),
            exact=True,
        )

        actual_count = int(result.count)

        status = "PASS" if actual_count == expected_count else "FAIL"

        if status != "PASS":
            failed_artifacts.append(artifact_group)

        checks[artifact_group] = {
            "expected_count": expected_count,
            "actual_count": actual_count,
            "status": status,
        }

    return {
        "checks": checks,
        "failed_artifacts": failed_artifacts,
        "status": "PASS" if not failed_artifacts else "FAIL",
    }


def compact_search_result(point: Any) -> dict[str, Any]:
    payload = point.payload or {}

    return {
        "score": float(point.score),
        "artifact_group": payload.get("artifact_group"),
        "artifact_type": payload.get("artifact_type"),
        "artifact_id": payload.get("artifact_id"),
        "title": payload.get("title"),
        "issue_type": payload.get("issue_type"),
        "severity": payload.get("severity"),
        "order_id": payload.get("order_id"),
        "product_id": payload.get("product_id"),
        "seller_id": payload.get("seller_id"),
        "category_id": payload.get("category_id"),
        "text_preview": payload.get("text_preview"),
    }


def run_search_tests(
    client: QdrantClient,
    model: SentenceTransformer,
    collection_name: str,
) -> dict[str, Any]:
    results = {}
    failed_tests = []

    for test in SEARCH_TESTS:
        query_vector = model.encode(
            test["query"],
            normalize_embeddings=True,
        ).tolist()

        query_filter = build_artifact_filter(test["artifact_group"])

        points = qdrant_search(
            client=client,
            collection_name=collection_name,
            query_vector=query_vector,
            query_filter=query_filter,
            limit=test["limit"],
        )

        compact_results = [compact_search_result(point) for point in points]

        wrong_artifact_results = [
            result
            for result in compact_results
            if result["artifact_group"] != test["artifact_group"]
        ]

        status = (
            "PASS"
            if len(compact_results) > 0 and not wrong_artifact_results
            else "FAIL"
        )

        if status != "PASS":
            failed_tests.append(test["name"])

        results[test["name"]] = {
            "query": test["query"],
            "artifact_group_filter": test["artifact_group"],
            "result_count": len(compact_results),
            "wrong_artifact_result_count": len(wrong_artifact_results),
            "top_results": compact_results,
            "status": status,
        }

    return {
        "checks": results,
        "failed_tests": failed_tests,
        "status": "PASS" if not failed_tests else "FAIL",
    }


def validate_qdrant(output_path: Path) -> dict[str, Any]:
    settings = load_settings()

    client = build_qdrant_client(settings)

    print("Checking Qdrant collection...")
    collection_checks = get_collection_checks(
        client=client,
        collection_name=settings["collection_name"],
        expected_dimension=settings["embedding_dimension"],
    )

    print("Checking artifact counts...")
    artifact_count_checks = get_artifact_count_checks(
        client=client,
        collection_name=settings["collection_name"],
    )

    print(f"Loading embedding model: {settings['embedding_model_name']}")
    model = SentenceTransformer(settings["embedding_model_name"])

    print("Running semantic search tests...")
    search_checks = run_search_tests(
        client=client,
        model=model,
        collection_name=settings["collection_name"],
    )

    failed_sections = []

    if collection_checks["status"] != "PASS":
        failed_sections.append("collection_checks")

    if artifact_count_checks["status"] != "PASS":
        failed_sections.append("artifact_count_checks")

    if search_checks["status"] != "PASS":
        failed_sections.append("search_checks")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collection_name": settings["collection_name"],
        "embedding_model_name": settings["embedding_model_name"],
        "summary": {
            "overall_status": "PASS" if not failed_sections else "FAIL",
            "failed_sections": failed_sections,
        },
        "collection_checks": collection_checks,
        "artifact_count_checks": artifact_count_checks,
        "search_checks": search_checks,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Qdrant collection, payload counts, and semantic retrieval."
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/qdrant_validation_report.json"),
        help="Path to save Qdrant validation report.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_qdrant(output_path=args.output)

    print("\nQdrant Validation Completed")
    print("---------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Collection: {report['collection_name']}")
    print(f"Embedding model: {report['embedding_model_name']}")
    print(f"Report saved to: {args.output}")

    print("\nCollection checks:")
    for key, value in report["collection_checks"].items():
        print(f"{key}: {value}")

    print("\nArtifact count checks:")
    for artifact_group, result in report["artifact_count_checks"]["checks"].items():
        print(
            f"{artifact_group}: "
            f"{result['actual_count']} / {result['expected_count']} "
            f"{result['status']}"
        )

    print("\nSearch checks:")
    for test_name, result in report["search_checks"]["checks"].items():
        top_score = (
            result["top_results"][0]["score"]
            if result["top_results"]
            else None
        )

        print(
            f"{test_name}: "
            f"{result['status']}, "
            f"results={result['result_count']}, "
            f"top_score={top_score}"
        )

    if report["summary"]["failed_sections"]:
        print(f"\nFailed sections: {report['summary']['failed_sections']}")


if __name__ == "__main__":
    main()