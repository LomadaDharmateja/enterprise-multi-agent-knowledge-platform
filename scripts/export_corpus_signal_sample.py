"""Export the committed evidence sample for the M1 corpus-signal CI test.

Why this exists: tests/test_corpus_signal.py is the real boundary test -- it reads
complaint counts from Qdrant and seller quality from PostgreSQL, so it fails if the
JSONL -> Qdrant handoff loses the linkage. But it is marked requires_stack, and CI
has no stack, so in CI it *skips*. A test that always skips is not a test that runs,
and shipping one as the M1 exit criterion would repeat the audit's own finding about
validators that cannot fail.

So this script snapshots what the live test reads into a committed fixture, and
tests/test_corpus_signal_ci.py recomputes the correlation from it with no stack at
all. CI then goes red if the committed corpus regresses.

Re-run this whenever the corpus is regenerated:

    python scripts/export_corpus_signal_sample.py
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT / "tests" / "baseline" / "corpus_signal_sample.json"

SAMPLE_SEED = 20260824
SAMPLE_SIZE = 400


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    client = QdrantClient(
        host=os.getenv("QDRANT_HOST", "localhost"),
        port=int(os.getenv("QDRANT_HTTP_PORT", "6333")),
    )
    collection = os.getenv("QDRANT_COLLECTION", "enterprise_knowledge")

    counts: collections.Counter[str] = collections.Counter()
    ticket_points = 0
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
            ticket_points += 1
            seller_id = payload.get("seller_id")
            if seller_id:
                counts[seller_id] += 1
        if offset is None:
            break

    url = (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'enterprise_user')}:"
        f"{os.getenv('POSTGRES_PASSWORD', 'enterprise_password')}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/"
        f"{os.getenv('POSTGRES_DB', 'enterprise_ai')}"
    )
    engine = create_engine(url)
    with engine.connect() as connection:
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
    engine.dispose()

    quality = {
        row[0]: {"n_orders": int(row[1]), "late_rate": float(row[2]), "mean_review": float(row[3])}
        for row in rows
    }

    sellers = sorted(quality)
    sample = random.Random(SAMPLE_SEED).sample(sellers, min(SAMPLE_SIZE, len(sellers)))

    payload = {
        "_comment": (
            "Snapshot of what tests/test_corpus_signal.py reads live. Regenerate with "
            "scripts/export_corpus_signal_sample.py after any corpus change."
        ),
        "sample_seed": SAMPLE_SEED,
        "sample_size": len(sample),
        "qdrant_ticket_points": ticket_points,
        "qdrant_ticket_points_with_seller_id": sum(counts.values()),
        "sellers": [
            {
                "seller_id": seller_id,
                "complaints": int(counts.get(seller_id, 0)),
                "n_orders": quality[seller_id]["n_orders"],
                "late_rate": quality[seller_id]["late_rate"],
                "mean_review": quality[seller_id]["mean_review"],
            }
            for seller_id in sample
        ],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out} ({len(sample)} sellers, {ticket_points} ticket points)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
