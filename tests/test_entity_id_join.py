"""Boundary tests for the entity-ID contract (F-01).

F-01: ingest read entity IDs from the top level of each record while the generator
writes them under `linked_entities`, so 0 of 8,152 points could join back to SQL or
graph evidence -- the defect that voided the project's architectural thesis.

The lesson from Task 1 applies here too: a payload-level assertion is necessary but
not sufficient. `test_vector_hit_joins_to_sql_row` runs a real vector query and takes
the returned seller_id to PostgreSQL, because "the ID is on the point" and "the ID
joins" are different claims and only the second one is the architecture.
"""

from __future__ import annotations

import os
import random

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.requires_stack

JOIN_KEYS = ["customer_id", "order_id", "product_id", "seller_id"]

# Order-scoped artifacts must carry the join keys. Policies and guides are scoped to
# a category, not an order; their absence is by design (docs/CORPUS_DESIGN.md Part 4).
GROUPS_REQUIRING_JOIN_KEYS = {
    "support_tickets",
    "customer_emails",
    "logistics_incidents",
    "warranty_claims",
}

SAMPLE_SEED = 20260824


@pytest.fixture(scope="module")
def qdrant(project_root):
    load_dotenv(project_root / ".env")

    from qdrant_client import QdrantClient

    collection = os.getenv("QDRANT_COLLECTION", "enterprise_knowledge")

    try:
        client = QdrantClient(
            host=os.getenv("QDRANT_HOST", "localhost"),
            port=int(os.getenv("QDRANT_HTTP_PORT", "6333")),
            api_key=os.getenv("QDRANT_API_KEY"),
            https=os.getenv("QDRANT_HTTPS", "false").lower() in {"true", "1", "yes"},
        )
        client.get_collection(collection)
    except Exception as exc:  # noqa: BLE001 -- any failure means "no stack"
        pytest.skip(f"Qdrant not reachable: {exc}")

    return client, collection


def scroll_all(qdrant, artifact_group=None):
    client, collection = qdrant

    query_filter = None

    if artifact_group is not None:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        query_filter = Filter(
            must=[
                FieldCondition(
                    key="artifact_group",
                    match=MatchValue(value=artifact_group),
                )
            ]
        )

    points = []
    offset = None

    while True:
        batch, offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=True,
            with_vectors=False,
            scroll_filter=query_filter,
        )

        if not batch:
            break

        points.extend(batch)

        if offset is None:
            break

    return points


def test_sampled_support_ticket_points_carry_entity_ids(qdrant):
    """A random sample of 50 support_tickets points all carry >=1 entity ID."""
    points = scroll_all(qdrant, artifact_group="support_tickets")

    assert len(points) >= 50, f"only {len(points)} support_tickets points"

    sample = random.Random(SAMPLE_SEED).sample(points, 50)

    without = [
        p.id for p in sample if not any((p.payload or {}).get(k) for k in JOIN_KEYS)
    ]

    assert not without, (
        f"{len(without)} of 50 sampled support_tickets points carry none of "
        f"{JOIN_KEYS} (F-01): {without[:5]}"
    )


def test_every_order_scoped_point_carries_entity_ids(qdrant):
    """The whole collection, not a sample -- 50 of 6,098 is not a coverage claim."""
    points = scroll_all(qdrant)

    assert points, "collection is empty"

    failures = {}

    for point in points:
        payload = point.payload or {}
        group = payload.get("artifact_group")

        if group not in GROUPS_REQUIRING_JOIN_KEYS:
            continue

        if not any(payload.get(k) for k in JOIN_KEYS):
            failures[group] = failures.get(group, 0) + 1

    assert not failures, f"points missing every entity ID, by group: {failures}"


def test_policy_points_carry_an_identifier(qdrant):
    """F-01's other half: the generator writes `document_id`, ingest read `policy_id`.

    All 40 policy documents previously reached Qdrant with no ID key at all.
    """
    points = scroll_all(qdrant, artifact_group="policy_documents")

    assert points, "no policy_documents points"

    without = [p.id for p in points if not (p.payload or {}).get("policy_id")]

    assert not without, f"{len(without)} of {len(points)} policies carry no policy_id"


def test_vector_hit_joins_to_sql_row(qdrant, connection):
    """The M2 exit criterion: a vector hit joins to a SQL row by entity ID.

    This runs the real retrieval path -- embed the question, query Qdrant, compact the
    result the way the retriever does -- then takes the seller_id and order_id off the
    top hits and looks them up in PostgreSQL. Nothing here reads the JSONL, so it fails
    if the linkage is lost anywhere between the generator and the retriever.
    """
    import sys

    from sentence_transformers import SentenceTransformer
    from sqlalchemy import text

    from hybrid_retriever import vector_retrieve

    client, collection = qdrant

    model = SentenceTransformer(
        os.getenv("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"),
        revision=os.getenv("EMBEDDING_MODEL_REVISION"),
    )

    retrieved = vector_retrieve(
        client=client,
        model=model,
        collection_name=collection,
        query="sellers with repeated late delivery complaints",
        per_group_limit=5,
        forced_artifact_groups=["support_tickets"],
    )

    hits = retrieved["results_by_artifact_group"]["support_tickets"]

    assert hits, "vector leg returned nothing"

    joined_sellers = 0
    joined_orders = 0

    for hit in hits:
        seller_id = hit.get("seller_id")
        order_id = hit.get("order_id")

        assert seller_id, f"vector hit carries no seller_id: {hit.get('artifact_id')}"

        seller_row = connection.execute(
            text(
                "SELECT seller_id, total_orders, avg_review_score, late_delivery_orders"
                " FROM ecommerce.vw_seller_performance WHERE seller_id = :seller_id"
            ),
            {"seller_id": seller_id},
        ).fetchone()

        assert seller_row is not None, (
            f"seller_id {seller_id} came back from Qdrant but matches no row in "
            "ecommerce.vw_seller_performance -- the vector hit does not join"
        )
        assert seller_row.seller_id == seller_id

        joined_sellers += 1

        order_row = connection.execute(
            text(
                "SELECT order_id, customer_id, order_status FROM ecommerce.orders"
                " WHERE order_id = :order_id"
            ),
            {"order_id": order_id},
        ).fetchone()

        assert order_row is not None, (
            f"order_id {order_id} from a Qdrant hit matches no row in ecommerce.orders"
        )

        # The join must agree across both databases, not merely resolve.
        assert order_row.customer_id == hit.get("customer_id"), (
            f"order {order_id}: Qdrant says customer {hit.get('customer_id')}, "
            f"PostgreSQL says {order_row.customer_id}"
        )

        joined_orders += 1

    assert joined_sellers == len(hits)
    assert joined_orders == len(hits)
