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


def test_sentiment_label_is_null_for_every_review(connection):
    """AUDIT.md F-05.

    scripts/clean_data.py emits a column called `sentiment`;
    postgres_loader.py:129 expects `sentiment_label`; standardize_columns()
    (postgres_loader.py:233-235) invents the missing column as NULL.
    """
    total, filled = _count(
        connection, "SELECT count(*), count(sentiment_label) FROM ecommerce.reviews"
    )
    assert total == 99224
    assert filled == 0, "sentiment_label is populated -- F-05 was fixed; update this test"


def test_category_english_name_is_null_for_every_category(connection):
    """AUDIT.md F-06.

    FILE_CANDIDATES (postgres_loader.py:54-58) does not list
    translations_cleaned.csv, so resolve_dataset_path found nothing and the loader
    synthesised the table from distinct product categories with no English column.
    reports/postgres_load_report.json records "source_file": null for this table.
    """
    total, filled = _count(
        connection,
        "SELECT count(*), count(product_category_name_english) "
        "FROM ecommerce.product_category_translations",
    )
    assert total == 73
    assert filled == 0, "english names are populated -- F-06 was fixed; update this test"


def test_shipment_status_is_null_for_every_order(connection):
    """Third instance of the same mechanism, found in M0 and not in the original audit.

    clean_data.py writes `shipping_status` (notebook cell 21);
    TABLE_COLUMNS["orders"] (postgres_loader.py:99-111) expects `shipment_status`;
    standardize_columns() invents it as NULL. The CSV's `delivery_delay_days` is
    dropped for the same reason -- it is not in TABLE_COLUMNS at all, so
    vw_order_summary recomputes it from the timestamps (views.sql:75).
    """
    total, filled = _count(
        connection, "SELECT count(*), count(shipment_status) FROM ecommerce.orders"
    )
    assert total == 99441
    assert filled == 0, "shipment_status is populated -- this defect was fixed; update this test"

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
