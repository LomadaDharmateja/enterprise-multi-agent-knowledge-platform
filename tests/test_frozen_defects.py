"""M0 freeze: known defects, asserted as they currently behave.

These tests PASS while the system is broken. That is the point. Each one pins a
defect the audit found so that fixing it in a later milestone is a deliberate,
visible edit to this file rather than a silent change in meaning. Deleting a test
from here is how a milestone records that it fixed something.

Everything here needs the live stack, so it is marked requires_stack and skips
when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.requires_stack


@pytest.fixture(scope="module")
def connection(project_root):
    load_dotenv(project_root / ".env")
    url = (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'enterprise_user')}:"
        f"{os.getenv('POSTGRES_PASSWORD', 'enterprise_password')}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/"
        f"{os.getenv('POSTGRES_DB', 'enterprise_ai')}"
    )
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            yield conn
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "no stack"
        pytest.skip(f"PostgreSQL not reachable: {exc}")


def _count(connection, sql: str) -> tuple:
    return connection.execute(text(sql)).one()


def test_row_counts_are_frozen(connection):
    expected = {
        "customers": 99441,
        "sellers": 3095,
        "products": 32340,
        "orders": 99441,
        "order_items": 111046,
        "payments": 103886,
        "reviews": 99224,
        "product_category_translations": 73,
    }
    actual = {
        table: _count(connection, f"SELECT count(*) FROM ecommerce.{table}")[0]
        for table in expected
    }
    assert actual == expected


def test_sentiment_label_is_populated_for_every_review(connection):
    """AUDIT.md F-05 -- FIXED in M1 task 4.

    Was: clean_data.py emits `sentiment`, TABLE_COLUMNS expects
    `sentiment_label`, and standardize_columns() invented the missing column as
    NULL for all 99,224 rows. Now a declared rename in COLUMN_RENAMES["reviews"],
    and standardize_columns() raises SchemaDriftError rather than inventing.
    """
    total, filled = _count(
        connection, "SELECT count(*), count(sentiment_label) FROM ecommerce.reviews"
    )
    assert total == 99224
    assert filled == 99224, f"F-05 regressed: {total - filled} NULL sentiment_label"

    labels = connection.execute(
        text("SELECT DISTINCT sentiment_label FROM ecommerce.reviews ORDER BY 1")
    ).scalars().all()
    assert labels == ["negative", "neutral", "positive"], labels


def test_category_english_name_is_populated_for_every_category(connection):
    """AUDIT.md F-04 -- FIXED in M1 task 4.

    Was: FILE_CANDIDATES did not list translations_cleaned.csv, the glob fallback
    required the substring "product_category_translations", so
    resolve_dataset_path(required=False) returned None and the loader built an
    empty frame; the outer merge then filled all 73 English names with NULL.

    M1 needs this: policy documents are titled from the English category name and
    are linked to sellers by category, so a NULL here reaches an LLM prompt as
    "None". Olist's own file covers 71 of 73; the two it omits are supplied by
    MANUAL_CATEGORY_TRANSLATIONS and the loader raises if any remain unfilled.
    """
    total, filled = _count(
        connection,
        "SELECT count(*), count(product_category_name_english) "
        "FROM ecommerce.product_category_translations",
    )
    assert total == 73
    assert filled == 73, f"F-04 regressed: {total - filled} categories with no english name"


def test_shipment_status_is_populated_for_every_order(connection):
    """Third instance of the same mechanism -- FIXED in M1 task 4.

    Found in M0, not in the original audit. clean_data.py writes
    `shipping_status`; TABLE_COLUMNS["orders"] expects `shipment_status`;
    standardize_columns() invented it as NULL for all 99,441 orders. Now a
    declared rename. The CSV's `delivery_delay_days` is still dropped, but
    deliberately -- it is not in TABLE_COLUMNS, and vw_order_summary recomputes
    it from the timestamps (views.sql:75).
    """
    total, filled = _count(
        connection, "SELECT count(*), count(shipment_status) FROM ecommerce.orders"
    )
    assert total == 99441
    assert filled == 99441, f"regressed: {total - filled} NULL shipment_status"

    columns = connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='ecommerce' AND table_name='orders'"
        )
    ).scalars().all()
    assert "delivery_delay_days" not in columns
    assert "shipping_status" not in columns


def test_ticket_orders_have_zero_variance_on_the_flagship_variables(connection):
    """AUDIT.md P1: every synthetic ticket sits on review_score=1 AND is_late_delivery.

    This is why the synthetic corpus carries no signal the structured tables do not
    already have. M1's exit criterion is that this test starts failing.
    """
    rows = connection.execute(
        text(
            "SELECT count(*) FROM ecommerce.vw_order_summary "
            "WHERE min_review_score = 1 AND is_late_delivery = true"
        )
    ).one()
    assert rows[0] > 0


def test_review_intelligence_limit_boundary_is_ambiguous(connection):
    """Found in M0 task 5-6; not in the original audit.

    review_intelligence is `ORDER BY review_score ASC ... LIMIT :limit` with no unique
    tiebreaker. 11,424 negative reviews tie at score 1, so which ten come back is left
    to PostgreSQL. Whether that actually varies depends on physical storage: on the
    freshly loaded clean-clone database the same query returned the same ten rows five
    times out of five, while on the original database -- aged by weeks of queries and
    the audit's write probes -- five runs returned three distinct result sets.

    So the runtime probe is not a reliable assertion. What IS always true, and is
    what this test pins, is that the query is under-determined: strictly more rows
    qualify at the boundary score than the LIMIT returns, so the ten rows the LLM
    reasons over are an arbitrary choice PostgreSQL is free to change.

    scripts/check_template_determinism.py measures the runtime behaviour on whatever
    database is in front of it.
    """
    limit = 10
    boundary_score = connection.execute(
        text(
            "SELECT review_score FROM ecommerce.vw_review_intelligence "
            "WHERE is_negative_review = TRUE "
            f"ORDER BY review_score ASC NULLS LAST LIMIT {limit}"
        )
    ).scalars().all()[-1]

    tied = connection.execute(
        text(
            "SELECT count(*) FROM ecommerce.vw_review_intelligence "
            "WHERE is_negative_review = TRUE AND review_score = :score"
        ),
        {"score": boundary_score},
    ).scalar_one()

    assert tied > limit, (
        f"only {tied} rows tie at the boundary score {boundary_score}; the LIMIT is no "
        "longer ambiguous -- a unique tiebreaker may have been added, so update this test"
    )
    assert tied > 10000, tied  # 11,424 at capture time
