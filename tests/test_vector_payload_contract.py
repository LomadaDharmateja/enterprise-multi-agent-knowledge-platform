"""Boundary test for the Qdrant payload -> retriever contract (F-02).

The audit's structural finding (F-12) was that twelve validators passed while the
retrieval layer was inert, because each validated its own stage. This test sits on
the seam: it reads a live Qdrant point and then pushes it through the retriever's
own ``compact_vector_result`` -- the function that was reading a key ingest never
wrote. Asserting on the payload alone would not have caught F-02; asserting on the
compacted result is what closes the contract.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.requires_stack


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


def scroll_points(qdrant, limit, artifact_group=None):
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

    points, _ = client.scroll(
        collection_name=collection,
        limit=limit,
        with_payload=True,
        with_vectors=False,
        scroll_filter=query_filter,
    )

    return points


def test_live_point_carries_non_empty_text(qdrant):
    """A known point read straight from Qdrant has readable document text."""
    points = scroll_points(qdrant, limit=1)

    assert points, "collection is empty"

    payload = points[0].payload or {}

    assert "text" in payload, f"payload has no 'text' key; keys={sorted(payload)}"
    assert isinstance(payload["text"], str)
    assert payload["text"].strip(), "payload['text'] is empty"


def test_every_sampled_point_carries_text(qdrant):
    """Not one lucky point -- the whole sample."""
    points = scroll_points(qdrant, limit=200)

    empty = [
        p.id
        for p in points
        if not isinstance((p.payload or {}).get("text"), str)
        or not (p.payload or {})["text"].strip()
    ]

    assert not empty, f"{len(empty)} of {len(points)} points carry no text: {empty[:5]}"


def test_retriever_compaction_delivers_the_text(qdrant):
    """The F-02 contract: what the retriever hands downstream must contain the text.

    ``compact_vector_result`` read ``payload['text_preview']``, a key ingest never
    wrote, so this assertion is the one that fails on the unfixed code.
    """
    from hybrid_retriever import compact_vector_result

    points = scroll_points(qdrant, limit=25)

    assert points, "collection is empty"

    class ScoredPoint:
        """scroll() returns records without a score; compaction requires one."""

        def __init__(self, point):
            self.payload = point.payload
            self.score = 0.5

    compacted = [compact_vector_result(ScoredPoint(p)) for p in points]

    missing_text = [c for c in compacted if not (c.get("text_preview") or "").strip()]
    missing_id = [c for c in compacted if not c.get("artifact_id")]

    assert not missing_text, (
        f"{len(missing_text)} of {len(compacted)} compacted results reach the LLM "
        "with no document text (F-02)"
    )
    assert not missing_id, (
        f"{len(missing_id)} of {len(compacted)} compacted results have no artifact_id, "
        "so nothing in the answer can be cited back to a document"
    )
