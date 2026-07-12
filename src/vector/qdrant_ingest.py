from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer
from tqdm import tqdm


DEFAULT_SYNTHETIC_DIR = Path("data/synthetic")
DEFAULT_REPORT_PATH = Path("reports/qdrant_ingestion_report.json")

ARTIFACT_FILES = {
    "support_tickets": "support_tickets.jsonl",
    "logistics_incidents": "logistics_incidents.jsonl",
    "customer_emails": "customer_emails.jsonl",
    "warranty_claims": "warranty_claims.jsonl",
    "policy_documents": "policy_documents.jsonl",
    "troubleshooting_guides": "troubleshooting_guides.jsonl",
}


def load_settings() -> dict[str, Any]:
    load_dotenv()

    return {
        "qdrant_host": os.getenv("QDRANT_HOST", "localhost"),
        "qdrant_http_port": int(os.getenv("QDRANT_HTTP_PORT", "6333")),
        "qdrant_collection": os.getenv("QDRANT_COLLECTION", "enterprise_knowledge"),
        "embedding_model_name": os.getenv(
            "EMBEDDING_MODEL_NAME",
            "sentence-transformers/all-MiniLM-L6-v2",
        ),
        "embedding_dimension": int(os.getenv("EMBEDDING_DIMENSION", "384")),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []

    if not path.exists():
        raise FileNotFoundError(f"Missing synthetic artifact file: {path}")

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {path} at line {line_number}: {exc}"
                ) from exc

    return records


def first_existing_value(record: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = record.get(key)

        if value is not None and value != "":
            return value

    return None


def extract_entity_ids(record: dict[str, Any]) -> dict[str, Any]:
    entity_keys = [
        "customer_id",
        "customer_unique_id",
        "order_id",
        "order_item_id",
        "product_id",
        "seller_id",
        "review_id",
        "payment_id",
        "ticket_id",
        "incident_id",
        "email_id",
        "claim_id",
        "policy_id",
        "guide_id",
        "category_id",
        "category_name",
        "product_category_name",
        "region_id",
        "city",
        "state",
    ]

    entity_ids = {}

    for key in entity_keys:
        value = record.get(key)

        if value is not None and value != "":
            entity_ids[key] = value

    return entity_ids


def normalize_value(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)

    return str(value)


def build_document_text(
    artifact_type: str,
    record: dict[str, Any],
) -> str:
    title = first_existing_value(
        record,
        [
            "title",
            "subject",
            "policy_title",
            "guide_title",
            "ticket_subject",
            "email_subject",
            "claim_subject",
            "incident_subject",
        ],
    )

    description = first_existing_value(
        record,
        [
            "description",
            "summary",
            "body",
            "message",
            "content",
            "comment",
            "review_comment_message",
            "issue_description",
            "resolution",
            "policy_text",
            "guide_text",
            "troubleshooting_steps",
            "root_cause",
            "customer_message",
            "agent_notes",
        ],
    )

    fields_for_text = [
        "ticket_id",
        "incident_id",
        "email_id",
        "claim_id",
        "policy_id",
        "guide_id",
        "customer_id",
        "order_id",
        "product_id",
        "seller_id",
        "category_id",
        "category_name",
        "product_category_name",
        "region_id",
        "city",
        "state",
        "severity",
        "priority",
        "status",
        "sentiment",
        "issue_type",
        "case_type",
        "policy_type",
        "guide_type",
        "refund_status",
        "warranty_status",
        "delivery_status",
        "title",
        "subject",
        "description",
        "summary",
        "body",
        "message",
        "content",
        "comment",
        "review_comment_message",
        "issue_description",
        "resolution",
        "policy_text",
        "guide_text",
        "troubleshooting_steps",
        "root_cause",
        "customer_message",
        "agent_notes",
    ]

    text_parts = [f"Artifact type: {artifact_type}"]

    if title:
        text_parts.append(f"Title: {normalize_value(title)}")

    if description:
        text_parts.append(f"Description: {normalize_value(description)}")

    for key in fields_for_text:
        value = record.get(key)

        if value is None or value == "":
            continue

        text_parts.append(f"{key}: {normalize_value(value)}")

    return "\n".join(text_parts)


def build_payload(
    artifact_type: str,
    source_file: str,
    record: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    entity_ids = extract_entity_ids(record)

    payload = {
        "artifact_type": artifact_type,
        "artifact_group": artifact_type,
        "document_type": artifact_type,
        "source_file": source_file,
        "text": text,
        "entity_ids": entity_ids,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

    important_keys = [
        "customer_id",
        "customer_unique_id",
        "order_id",
        "order_item_id",
        "product_id",
        "seller_id",
        "review_id",
        "payment_id",
        "ticket_id",
        "incident_id",
        "email_id",
        "claim_id",
        "policy_id",
        "guide_id",
        "category_id",
        "category_name",
        "product_category_name",
        "region_id",
        "city",
        "state",
        "severity",
        "priority",
        "status",
        "sentiment",
        "issue_type",
        "case_type",
        "policy_type",
        "guide_type",
        "refund_status",
        "warranty_status",
        "delivery_status",
        "created_at",
        "updated_at",
    ]

    for key in important_keys:
        value = record.get(key)

        if value is not None and value != "":
            payload[key] = value

    title = first_existing_value(
        record,
        [
            "title",
            "subject",
            "policy_title",
            "guide_title",
            "ticket_subject",
            "email_subject",
            "claim_subject",
            "incident_subject",
        ],
    )

    if title:
        payload["title"] = normalize_value(title)

    return payload


def get_record_identifier(
    artifact_type: str,
    record: dict[str, Any],
    fallback_index: int,
) -> str:
    id_keys_by_artifact = {
        "support_tickets": ["ticket_id"],
        "logistics_incidents": ["incident_id"],
        "customer_emails": ["email_id"],
        "warranty_claims": ["claim_id"],
        "policy_documents": ["policy_id"],
        "troubleshooting_guides": ["guide_id"],
    }

    candidate_keys = id_keys_by_artifact.get(artifact_type, [])

    candidate_keys += [
        "id",
        "document_id",
        "order_id",
        "product_id",
        "seller_id",
    ]

    value = first_existing_value(record, candidate_keys)

    if value:
        return f"{artifact_type}:{value}"

    return f"{artifact_type}:row:{fallback_index}"


def make_qdrant_point_id(stable_document_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, stable_document_id))


def load_synthetic_documents(synthetic_dir: Path) -> list[dict[str, Any]]:
    documents = []

    for artifact_type, filename in ARTIFACT_FILES.items():
        path = synthetic_dir / filename
        records = read_jsonl(path)

        for index, record in enumerate(records):
            stable_document_id = get_record_identifier(
                artifact_type=artifact_type,
                record=record,
                fallback_index=index,
            )

            text = build_document_text(
                artifact_type=artifact_type,
                record=record,
            )

            payload = build_payload(
                artifact_type=artifact_type,
                source_file=filename,
                record=record,
                text=text,
            )

            documents.append(
                {
                    "point_id": make_qdrant_point_id(stable_document_id),
                    "stable_document_id": stable_document_id,
                    "artifact_type": artifact_type,
                    "text": text,
                    "payload": payload,
                }
            )

    return documents


def create_qdrant_client(host: str, port: int) -> QdrantClient:
    return QdrantClient(
        host=host,
        port=port,
        timeout=120,
    )


def get_model_dimension(model: SentenceTransformer) -> int:
    if hasattr(model, "get_embedding_dimension"):
        dimension = model.get_embedding_dimension()
    else:
        dimension = model.get_sentence_embedding_dimension()

    if dimension is None:
        raise ValueError("Could not determine embedding dimension from model.")

    return int(dimension)


def ensure_qdrant_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
    recreate: bool = True,
) -> None:
    existing_collections = client.get_collections().collections
    existing_collection_names = {
        collection.name for collection in existing_collections
    }

    if collection_name in existing_collection_names:
        if recreate:
            print(f"Deleting existing Qdrant collection: {collection_name}")
            client.delete_collection(collection_name=collection_name)
        else:
            print(f"Qdrant collection already exists: {collection_name}")
            return
    else:
        print(f"Qdrant collection does not exist yet: {collection_name}")

    print(
        f"Creating Qdrant collection: {collection_name} "
        f"with vector size {vector_size}"
    )

    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=vector_size,
            distance=Distance.COSINE,
        ),
    )


def count_by_artifact_type(documents: list[dict[str, Any]]) -> dict[str, int]:
    counts = {}

    for document in documents:
        artifact_type = document["artifact_type"]
        counts[artifact_type] = counts.get(artifact_type, 0) + 1

    return counts


def ingest_documents(
    synthetic_dir: Path,
    report_path: Path,
    batch_size: int,
    recreate_collection: bool,
) -> dict[str, Any]:
    settings = load_settings()

    collection_name = settings["qdrant_collection"]
    expected_dimension = settings["embedding_dimension"]

    print("Loading synthetic documents...")
    documents = load_synthetic_documents(synthetic_dir)
    print(f"Loaded {len(documents)} documents from JSONL files.")

    if not documents:
        raise ValueError("No synthetic documents found for Qdrant ingestion.")

    print(f"Loading embedding model: {settings['embedding_model_name']}")
    model = SentenceTransformer(settings["embedding_model_name"])

    actual_dimension = get_model_dimension(model)

    if actual_dimension != expected_dimension:
        raise ValueError(
            f"Embedding dimension mismatch. "
            f"Expected {expected_dimension}, got {actual_dimension}."
        )

    client = create_qdrant_client(
        host=settings["qdrant_host"],
        port=settings["qdrant_http_port"],
    )

    ensure_qdrant_collection(
        client=client,
        collection_name=collection_name,
        vector_size=actual_dimension,
        recreate=recreate_collection,
    )

    print("Embedding and upserting documents into Qdrant...")

    total_batches = (len(documents) + batch_size - 1) // batch_size

    for start_index in tqdm(
        range(0, len(documents), batch_size),
        total=total_batches,
    ):
        batch = documents[start_index : start_index + batch_size]

        texts = [document["text"] for document in batch]

        embeddings = model.encode(
            texts,
            batch_size=min(batch_size, 64),
            show_progress_bar=False,
            normalize_embeddings=True,
        )

        points = []

        for document, embedding in zip(batch, embeddings):
            payload = dict(document["payload"])
            payload["stable_document_id"] = document["stable_document_id"]

            points.append(
                PointStruct(
                    id=document["point_id"],
                    vector=embedding.tolist(),
                    payload=payload,
                )
            )

        client.upsert(
            collection_name=collection_name,
            points=points,
            wait=True,
        )

    collection_info = client.get_collection(collection_name=collection_name)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": "PASS",
        "collection_name": collection_name,
        "qdrant_host": settings["qdrant_host"],
        "qdrant_http_port": settings["qdrant_http_port"],
        "embedding_model_name": settings["embedding_model_name"],
        "embedding_dimension": actual_dimension,
        "document_count": len(documents),
        "artifact_counts": count_by_artifact_type(documents),
        "qdrant_points_count": collection_info.points_count,
        "recreated_collection": recreate_collection,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)

    with report_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False, default=str)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest synthetic enterprise documents into Qdrant."
    )

    parser.add_argument(
        "--synthetic-dir",
        type=Path,
        default=DEFAULT_SYNTHETIC_DIR,
        help="Directory containing synthetic JSONL artifact files.",
    )

    parser.add_argument(
        "--report-path",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Path to save Qdrant ingestion report.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=128,
        help="Number of documents to embed and upsert per batch.",
    )

    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not recreate the Qdrant collection if it already exists.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = ingest_documents(
        synthetic_dir=args.synthetic_dir,
        report_path=args.report_path,
        batch_size=args.batch_size,
        recreate_collection=not args.no_recreate,
    )

    print("\nQdrant Ingestion Completed")
    print("--------------------------")
    print(f"Overall status: {report['overall_status']}")
    print(f"Collection: {report['collection_name']}")
    print(f"Embedding model: {report['embedding_model_name']}")
    print(f"Embedding dimension: {report['embedding_dimension']}")
    print(f"Documents ingested: {report['document_count']}")
    print(f"Qdrant points count: {report['qdrant_points_count']}")
    print(f"Report saved to: {args.report_path}")

    print("\nArtifact counts:")

    for artifact_type, count in report["artifact_counts"].items():
        print(f"- {artifact_type}: {count}")


if __name__ == "__main__":
    main()