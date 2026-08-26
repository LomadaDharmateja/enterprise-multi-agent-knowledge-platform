"""The runtime PostgreSQL role must be able to read and nothing else (M6 Task 1).

AUDIT.md P2:

    `enterprise_user` is a PostgreSQL **superuser** with `Create role, Create DB,
    Replication, Bypass RLS`. `records_query` uses `engine.begin()`, a committing
    read-write transaction -- I passed a row-returning `INSERT` through the project's
    own helper and confirmed on a fresh connection that it committed.

These are the audit's own write probes, re-run against the role the runtime now uses.
Each asserts a *permission denial*, not merely that nothing happened: a write that
silently no-ops is indistinguishable from a write that succeeded and was rolled back,
and only one of those is a security control.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.requires_stack

INSUFFICIENT_PRIVILEGE = "42501"  # SQLSTATE for permission denied


@pytest.fixture(scope="module")
def readonly_engine(project_root):
    load_dotenv(project_root / ".env")

    from sqlalchemy import create_engine, text

    user = os.getenv("POSTGRES_READONLY_USER")
    password = os.getenv("POSTGRES_READONLY_PASSWORD")

    if not user or not password:
        pytest.skip("POSTGRES_READONLY_USER/PASSWORD not configured")

    url = (
        f"postgresql+psycopg2://{user}:{password}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/"
        f"{os.getenv('POSTGRES_DB', 'enterprise_ai')}"
    )

    try:
        engine = create_engine(url)

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"read-only role not reachable: {exc}")

    yield engine

    engine.dispose()


def attempt(engine, statement: str):
    """Run a statement and return the raised exception, or None if it succeeded."""
    from sqlalchemy import text

    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    except Exception as exc:  # noqa: BLE001 -- the exception is the assertion subject
        return exc

    return None


def assert_permission_denied(error, statement: str):
    assert error is not None, (
        f"the read-only role executed `{statement}` without error -- this is the "
        "audit's finding reproduced, not fixed"
    )

    text = str(error).lower()
    code = getattr(getattr(error, "orig", None), "pgcode", None)

    assert code == INSUFFICIENT_PRIVILEGE or "permission denied" in text, (
        f"`{statement}` failed, but not with a permission error. SQLSTATE={code}. "
        f"A failure for the wrong reason is not a security control: {error}"
    )


# --------------------------------------------------------------------------------
# The role can read
# --------------------------------------------------------------------------------


def test_the_readonly_role_can_select(readonly_engine):
    from sqlalchemy import text

    with readonly_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM ecommerce.sellers")
        ).scalar()

    assert count > 0, "the read-only role cannot read; it is locked out, not secured"


def test_the_readonly_role_can_read_every_view_the_runtime_uses(readonly_engine):
    from sqlalchemy import text

    views = [
        "vw_seller_performance", "vw_product_performance", "vw_review_intelligence",
        "vw_payment_summary", "vw_customer_order_history", "vw_order_summary",
    ]

    with readonly_engine.connect() as connection:
        for view in views:
            connection.execute(text(f"SELECT 1 FROM ecommerce.{view} LIMIT 1"))


# --------------------------------------------------------------------------------
# The role cannot write -- the audit's four probes
# --------------------------------------------------------------------------------


def test_insert_is_denied(readonly_engine):
    statement = (
        "INSERT INTO ecommerce.sellers "
        "(seller_id, seller_city, seller_state, seller_zip_code_prefix) "
        "VALUES ('m6probe', 'nowhere', 'XX', 0)"
    )
    assert_permission_denied(attempt(readonly_engine, statement), statement)


def test_update_is_denied(readonly_engine):
    statement = "UPDATE ecommerce.sellers SET seller_city = 'tampered'"
    assert_permission_denied(attempt(readonly_engine, statement), statement)


def test_delete_is_denied(readonly_engine):
    statement = "DELETE FROM ecommerce.sellers"
    assert_permission_denied(attempt(readonly_engine, statement), statement)


def test_drop_is_denied(readonly_engine):
    statement = "DROP TABLE ecommerce.sellers"
    assert_permission_denied(attempt(readonly_engine, statement), statement)


# --------------------------------------------------------------------------------
# Adjacent holes a "read-only" role commonly still has
# --------------------------------------------------------------------------------


def test_creating_a_table_in_the_ecommerce_schema_is_denied(readonly_engine):
    statement = "CREATE TABLE ecommerce.m6probe (id int)"
    assert_permission_denied(attempt(readonly_engine, statement), statement)


def test_creating_a_table_in_the_public_schema_is_denied(readonly_engine):
    """The classic hole: PUBLIC can CREATE in `public` by default."""
    statement = "CREATE TABLE public.m6probe (id int)"
    assert_permission_denied(attempt(readonly_engine, statement), statement)


def test_truncate_is_denied(readonly_engine):
    statement = "TRUNCATE ecommerce.reviews"
    assert_permission_denied(attempt(readonly_engine, statement), statement)


def test_the_row_returning_insert_from_the_audit_is_denied(readonly_engine):
    """The exact shape the audit smuggled through `records_query`.

    It returns rows, so it looks like a SELECT to a helper that only checks whether
    the result has rows.
    """
    statement = (
        "INSERT INTO ecommerce.sellers "
        "(seller_id, seller_city, seller_state, seller_zip_code_prefix) "
        "VALUES ('m6probe2', 'nowhere', 'XX', 0) RETURNING seller_id"
    )
    assert_permission_denied(attempt(readonly_engine, statement), statement)


# --------------------------------------------------------------------------------
# Role attributes
# --------------------------------------------------------------------------------


def test_the_role_holds_no_superuser_attributes(readonly_engine):
    from sqlalchemy import text

    with readonly_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
                "rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone()

    assert row is not None
    assert not any(row), f"the runtime role holds elevated attributes: {tuple(row)}"


def test_the_role_holds_only_select_grants(readonly_engine):
    from sqlalchemy import text

    with readonly_engine.connect() as connection:
        privileges = {
            r[0]
            for r in connection.execute(
                text(
                    "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = current_user AND table_schema = 'ecommerce'"
                )
            )
        }

    assert privileges == {"SELECT"}, f"unexpected privileges granted: {privileges}"


def test_the_runtime_settings_select_the_readonly_role(project_root):
    """The role is worthless if the retrieval layer still connects as the owner."""
    load_dotenv(project_root / ".env")

    from hybrid_retriever import load_settings

    assert load_settings()["postgres_user"] == os.getenv("POSTGRES_READONLY_USER")
