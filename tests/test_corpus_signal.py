"""M1 boundary test: does the synthetic layer carry signal SQL does not have?

This is the test that decides whether M1 is done. It crosses a stage boundary on
purpose -- AUDIT.md F-12's lesson was that twelve validators all passed while the
retrieval layer was inert, because each checked its own stage and none checked a
seam. So complaint counts are read from **Qdrant payload metadata**, not from the
generator's own JSONL, and seller quality is read from **PostgreSQL**. If ingest
drops the seller IDs again (F-01), this test fails rather than quietly passing.

What it asserts, and why the band rather than "as high as possible":

    0.3 <= abs(spearman(complaints_per_seller, seller_late_delivery_rate)) <= 0.6

The upper bound is not a formality. v1 scored +0.867 against order volume, which
made the corpus a restatement of `count(orders)`; allocating complaints straight
from a SQL-computable quality score instead gives ~0.90, which makes it a
restatement of `avg(review_score)`. Either way PostgreSQL recovers the corpus
exactly and the vector layer carries nothing new. A correlation *above* the band
is as much a failure as one below it. See docs/CORPUS_DESIGN.md Part 2 section 1.
"""

from __future__ import annotations

import collections
import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.requires_stack


#: Committed so the drawn sample is identical on every run. n=400 is a
#: measurement, not a round number: simulating this test 300 times against a
#: corpus known to be correct, n=100 landed outside the band on 13% of runs and
#: n=400 on none. docs/CORPUS_DESIGN.md Part 3 D-3.
SAMPLE_SEED = 20260824
SAMPLE_SIZE = 400

BAND_LOW = 0.3
BAND_HIGH = 0.6

#: AUDIT.md P1 measured this null by drawing sellers uniformly at random.
UNIFORM_NULL_RHO = 0.017


@pytest.fixture(scope="module")
def qdrant_complaint_counts():
    """Complaints per seller, read from Qdrant payloads."""
    load_dotenv()
    qdrant_client = pytest.importorskip("qdrant_client")

    client = qdrant_client.QdrantClient(
        host=os.getenv("QDRANT_HOST", "localhost"),
        port=int(os.getenv("QDRANT_HTTP_PORT", "6333")),
    )
    collection = os.getenv("QDRANT_COLLECTION", "enterprise_knowledge")

    try:
        client.get_collection(collection)
    except Exception as exc:  # noqa: BLE001 -- any failure means "no stack"
        pytest.skip(f"Qdrant collection {collection!r} unavailable: {exc}")

    counts: collections.Counter[str] = collections.Counter()
    scanned = 0
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=1000,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            if payload.get("artifact_group") != "support_tickets":
                continue
            scanned += 1
            seller_id = payload.get("seller_id")
            if seller_id:
                counts[seller_id] += 1
        if offset is None:
            break

    if scanned == 0:
        pytest.skip("no support_ticket points in Qdrant; run qdrant_ingest.py")

    return {"counts": counts, "ticket_points": scanned}


@pytest.fixture(scope="module")
def seller_quality(connection):
    """Late-delivery rate and review average per seller, from PostgreSQL."""
    from sqlalchemy import text

    rows = connection.execute(
        text(
            """
            WITH seller_orders AS (
                SELECT DISTINCT oi.seller_id, oi.order_id
                FROM ecommerce.order_items oi
                WHERE oi.seller_id IS NOT NULL
            )
            SELECT so.seller_id,
                   count(*)                                       AS n_orders,
                   avg((os.is_late_delivery IS TRUE)::int)::float AS late_rate,
                   avg(r.review_score)::float                     AS mean_review
            FROM seller_orders so
            JOIN ecommerce.vw_order_summary os ON os.order_id = so.order_id
            LEFT JOIN ecommerce.reviews r ON r.order_id = so.order_id
            GROUP BY 1
            HAVING avg(r.review_score) IS NOT NULL
            ORDER BY so.seller_id
            """
        )
    ).fetchall()
    return {row[0]: {"n_orders": row[1], "late_rate": row[2], "mean_review": row[3]} for row in rows}


def _sample(seller_quality):
    import random

    sellers = sorted(seller_quality)
    rng = random.Random(SAMPLE_SEED)
    return rng.sample(sellers, min(SAMPLE_SIZE, len(sellers)))


def test_qdrant_points_carry_seller_ids(qdrant_complaint_counts):
    """AUDIT.md F-01 regression guard, and a precondition for everything below.

    0 of 8,152 v1 points carried a seller_id, so the correlation this module
    measures was not computable from the vector layer at all.
    """
    counts = qdrant_complaint_counts["counts"]
    assert counts, (
        "No support_ticket point in Qdrant carries a top-level seller_id. "
        "This is AUDIT.md F-01: the vector layer cannot join back to SQL."
    )
    assert sum(counts.values()) > 0.95 * qdrant_complaint_counts["ticket_points"], (
        f"only {sum(counts.values())} of {qdrant_complaint_counts['ticket_points']} "
        f"ticket points carry a seller_id"
    )


def test_complaint_counts_correlate_with_seller_quality(
    qdrant_complaint_counts, seller_quality
):
    """The M1 exit criterion.

    Complaint counts come from Qdrant metadata; quality comes from PostgreSQL.
    Nothing in this assertion reads the generator's output, so it fails if the
    JSONL -> Qdrant handoff loses the linkage.
    """
    spearmanr = pytest.importorskip("scipy.stats").spearmanr

    counts = qdrant_complaint_counts["counts"]
    sample = _sample(seller_quality)

    complaints = [counts.get(seller_id, 0) for seller_id in sample]
    late_rate = [seller_quality[seller_id]["late_rate"] for seller_id in sample]

    rho = spearmanr(complaints, late_rate).statistic

    assert BAND_LOW <= abs(rho) <= BAND_HIGH, (
        f"rho(complaints, late_delivery_rate) = {rho:+.4f} on a seeded sample of "
        f"{len(sample)} sellers, outside the required band [{BAND_LOW}, {BAND_HIGH}].\n"
        f"  Below {BAND_LOW}: the corpus carries no real signal (v1's uniform null "
        f"was {UNIFORM_NULL_RHO}).\n"
        f"  Above {BAND_HIGH}: the corpus is recoverable from a PostgreSQL column "
        f"and the vector layer is redundant (v1 scored +0.867 against order volume).\n"
        f"See docs/CORPUS_DESIGN.md Part 2 section 1."
    )


def test_complaints_are_not_merely_a_proxy_for_order_volume(
    qdrant_complaint_counts, seller_quality
):
    """AUDIT.md P1's actual finding, guarded directly.

    v1's rho(complaints, n_orders) was +0.867, so "which sellers have the most
    complaints" was to first order "which sellers have the most orders".
    """
    spearmanr = pytest.importorskip("scipy.stats").spearmanr

    counts = qdrant_complaint_counts["counts"]
    sample = _sample(seller_quality)

    complaints = [counts.get(seller_id, 0) for seller_id in sample]
    n_orders = [seller_quality[seller_id]["n_orders"] for seller_id in sample]
    late_rate = [seller_quality[seller_id]["late_rate"] for seller_id in sample]

    rho_volume = spearmanr(complaints, n_orders).statistic
    rho_quality = spearmanr(complaints, late_rate).statistic

    assert rho_volume < 0.6, (
        f"rho(complaints, n_orders) = {rho_volume:+.4f}; v1's was +0.867. "
        f"Complaint volume is tracking order volume rather than seller quality."
    )
    assert abs(rho_quality) > rho_volume - 0.15, (
        f"quality correlation ({rho_quality:+.4f}) is materially weaker than the "
        f"volume correlation ({rho_volume:+.4f}); the corpus is volume-driven."
    )


def test_complaints_land_on_well_rated_sellers_too(
    qdrant_complaint_counts, seller_quality
):
    """A complaint must not imply a bad seller.

    v1 put every one of its 3,000 tickets on an order with review_score = 1 and
    is_late_delivery = True -- zero variance, so there was nothing for retrieval
    to discriminate.
    """
    counts = qdrant_complaint_counts["counts"]
    well_rated_with_complaints = [
        seller_id
        for seller_id, attributes in seller_quality.items()
        if attributes["mean_review"] >= 4.0 and counts.get(seller_id, 0) > 0
    ]
    complaints_on_well_rated = sum(
        counts[seller_id] for seller_id in well_rated_with_complaints
    )
    total_complaints = sum(counts.values())

    share = complaints_on_well_rated / total_complaints
    assert 0.20 <= share <= 0.70, (
        f"{share:.1%} of complaints land on sellers rated >= 4.0. Outside "
        f"[20%, 70%] the corpus has either collapsed onto bad sellers (v1) or "
        f"stopped tracking quality at all."
    )
