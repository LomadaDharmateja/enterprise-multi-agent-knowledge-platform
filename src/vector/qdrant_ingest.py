from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer
from tqdm import tqdm


SYNTHETIC_FILES = {
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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []

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


def stable_uuid(text: str) -> str:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return (
        f"{digest[0:8]}-"
        f"{digest[8:12]}-"
        f"{digest[12:16]}-"
        f"{digest[16:20]}-"
        f"{digest[20:32]}"
    )


def get_nested(record: dict[str, Any], field: str) -> Any:
    current: Any = record

    for part in field.split("."):
        if not isinstance(current, dict):
            return None

        current = current.get(part)

    return current


def compact_payload_value(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, list):
        return [
            item
            for item in value
            if isinstance(item, (str, int, float, bool))
        ]

    return json.dumps(value, ensure_ascii=False, default=str)


def infer_artifact_id(artifact_name: str, record: dict[str, Any]) -> str:
    id_field = ID_FIELDS[artifact_name]
    artifact_id = record.get(id_field)

    if artifact_id is None:
        raise ValueError(f"Missing {id_field} for artifact type {artifact_name}")

    return str(artifact_id)


def build_document(record: dict[str, Any]) -> str:
    document_text = record.get("document_text")

    if document_text and str(document_text).strip():
        return str(document_text).strip()

    title = record.get("title") or record.get("subject") or ""
    body = record.get("body") or record.get("summary") or ""

    fallback = f"{title}\n{body}".strip()

    if not fallback:
        raise ValueError("Record has no document_text or fallback text.")

    return fallback


def build_payload(
    artifact_name: str,
    artifact_id: str,
    record: dict[str, Any],
    source_file: str,
) -> dict[str, Any]:
    linked_entities = record.get("linked_entities") or {}
    metadata = record.get("metadata") or {}

    payload = {
        "artifact_type": compact_payload_value(record.get("artifact_type")),
        "artifact_group": artifact_name,
        "artifact_id": artifact_id,
        "source_file": source_file,
        "title": compact_payload_value(record.get("title") or record.get("subject")),
        "created_at": compact_payload_value(record.get("created_at")),
        "issue_type": compact_payload_value(record.get("issue_type") or metadata.get("issue_type")),
        "severity": compact_payload_value(record.get("severity") or metadata.get("severity")),
        "status": compact_payload_value(record.get("status") or record.get("claim_status")),
        "policy_topic": compact_payload_value(record.get("policy_topic")),
        "document_scope": compact_payload_value(metadata.get("document_scope")),
        "customer_id": compact_payload_value(linked_entities.get("customer_id")),
        "customer_unique_id": compact_payload_value(linked_entities.get("customer_unique_id")),
        "order_id": compact_payload_value(linked_entities.get("order_id")),
        "product_id": compact_payload_value(linked_entities.get("product_id")),
        "seller_id": compact_payload_value(linked_entities.get("seller_id")),
        "review_id": compact_payload_value(linked_entities.get("review_id")),
        "ticket_id": compact_payload_value(record.get("ticket_id") or linked_entities.get("ticket_id")),
        "category_id": compact_payload_value(linked_entities.get("category_id") or metadata.get("category_id")),
        "category_name_english": compact_payload_value(
            linked_entities.get("category_name_english") or metadata.get("category_name_english")
        ),
        "region_id": compact_payload_value(linked_entities.get("region_id")),
        "customer_state": compact_payload_value(linked_entities.get("customer_state")),
        "customer_city": compact_payload_value(linked_entities.get("customer_city")),
        "text_preview": build_document(record)[:500],
    }

    return {
        key: value
        for key, value in payload.items()
        if value is not None
    }


def load_documents(synthetic_dir: Path) -> list[dict[str, Any]]:
    documents = []

    for artifact_name, filename in SYNTHETIC_FILES.items():
        path = synthetic_dir / filename
        records = load_jsonl(path)

        for record in records:
            artifact_id = infer_artifact_id(artifact_name, record)
            document_text = build_document(record)

            documents.append(
                {
                    "point_id": stable_uuid(f"{artifact_name}:{artifact_id}"),
                    "artifact_name": artifact_name,
                    "artifact_id": artifact_id,
                    "document_text": document_text,
                    "payload": build_payload(
                        artifact_name=artifact_name,
                        artifact_id=artifact_id,
                        record=record,
                        source_file=str(path),
                    ),
                }
            )

    return documents


def build_qdrant_client(settings: dict[str, Any]) -> QdrantClient:
    return QdrantClient(
        host=settings["qdrant_host"],
        port=settings["qdrant_port"],
    )


def recreate_collection(
    client: QdrantClient,
    collection_name: str,
    embedding_dimension: int,
) -> None:
    if client.collection_exists(collection_name):
        client.delete_collection(collection_name=collection_name)

    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=embedding_dimension,
            distance=Distance.COSINE,
        ),
    )


def batch_iter(items: list[dict[str, Any]], batch_size: int):
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


def ingest_documents(
    synthetic_dir: Path,
    output_path: Path,
    recreate: bool,
    batch_size: int,
) -> dict[str, Any]:
    settings = load_settings()

    print("Loading synthetic documents...")
    documents = load_documents(synthetic_dir)
    print(f"Loaded {len(documents)} documents from JSONL files.")

    print(f"Loading embedding model: {settings['embedding_model_name']}")
    model = SentenceTransformer(settings["embedding_model_name"])

    actual_dimension = model.get_sentence_embedding_dimension()

    if actual_dimension != settings["embedding_dimension"]:
        raise ValueError(
            f"Embedding dimension mismatch. "
            f"Model dimension={actual_dimension}, "
            f"configured dimension={settings['embedding_dimension']}"
        )

    client = build_qdrant_client(settings)

    if recreate:
        print(f"Recreating Qdrant collection: {settings['collection_name']}")
        recreate_collection(
            client=client,
            collection_name=settings["collection_name"],
            embedding_dimension=settings["embedding_dimension"],
        )

    artifact_counts: dict[str, int] = {}

    for document in documents:
        artifact_counts[document["artifact_name"]] = (
            artifact_counts.get(document["artifact_name"], 0) + 1
        )

    total_upserted = 0

    print("Embedding and upserting documents into Qdrant...")

    for batch in tqdm(list(batch_iter(documents, batch_size))):
        texts = [item["document_text"] for item in batch]

        embeddings = model.encode(
            texts,
            batch_size=min(64, len(texts)),
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        points = []

        for item, embedding in zip(batch, embeddings):
            points.append(
                PointStruct(
                    id=item["point_id"],
                    vector=embedding.tolist(),
                    payload=item["payload"],
                )
            )

        client.upsert(
            collection_name=settings["collection_name"],
            points=points,
        )

        total_upserted += len(points)

    collection_info = client.get_collection(settings["collection_name"])
    qdrant_points_count = int(collection_info.points_count or 0)

    failed_checks = []

    if qdrant_points_count != len(documents):
        failed_checks.append(
            {
                "check": "point_count_match",
                "expected": len(documents),
                "actual": qdrant_points_count,
            }
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "synthetic_dir": str(synthetic_dir),
        "collection_name": settings["collection_name"],
        "embedding_model_name": settings["embedding_model_name"],
        "embedding_dimension": settings["embedding_dimension"],
        "summary": {
            "overall_status": "PASS" if not failed_checks else "FAIL",
            "documents_loaded": len(documents),
            "points_upserted": total_upserted,
            "qdrant_points_count": qdrant_points_count,
            "artifact_type_count": len(artifact_counts),
            "failed_checks": failed_checks,
        },
        "artifact_counts": artifact_counts,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Embed synthetic enterprise documents and ingest them into Qdrant."
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
        default=Path("reports/qdrant_ingestion_report.json"),
        help="Path to save Qdrant ingestion report.",
    )

    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and recreate the Qdrant collection before ingestion.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=128,
        help="Number of documents to embed and upload per batch.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = ingest_documents(
        synthetic_dir=args.synthetic_dir,
        output_path=args.output,
        recreate=args.recreate,
        batch_size=args.batch_size,
    )

    print("\nQdrant Ingestion Completed")
    print("--------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Collection: {report['collection_name']}")
    print(f"Embedding model: {report['embedding_model_name']}")
    print(f"Documents loaded: {report['summary']['documents_loaded']}")
    print(f"Points upserted: {report['summary']['points_upserted']}")
    print(f"Qdrant points count: {report['summary']['qdrant_points_count']}")
    print(f"Report saved to: {args.output}")

    print("\nArtifact counts:")
    for artifact_name, count in report["artifact_counts"].items():
        print(f"{artifact_name}: {count}")

    if report["summary"]["failed_checks"]:
        print(f"\nFailed checks: {report['summary']['failed_checks']}")


if __name__ == "__main__":
    main()