"""Shared fixtures.

The modules under src/ are executed as scripts and import their siblings by bare
module name after patching sys.path themselves. Tests import them the same way so
that what is under test is the code as it actually runs -- not a repackaged variant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"


def _add_src_dirs_to_syspath() -> None:
    for directory in sorted(p for p in SRC.iterdir() if p.is_dir() and p.name != "__pycache__"):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))


_add_src_dirs_to_syspath()


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def baseline_path() -> Path:
    return PROJECT_ROOT / "tests" / "baseline" / "baseline_results.json"


@pytest.fixture(scope="session")
def connection(project_root):
    """A live PostgreSQL connection, or a skip.

    Session-scoped and shared: test_corpus_signal.py needs it alongside
    test_frozen_defects.py, which defines its own module-scoped copy.
    """
    import os

    from dotenv import load_dotenv
    from sqlalchemy import create_engine, text

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
