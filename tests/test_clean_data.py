"""M0 task 3: the data-cleaning stage is in the repository and still correct.

Skipped unless data/raw holds the Olist CSVs (they are not committed -- see
docs/SETUP.md) and PostgreSQL is reachable.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.requires_stack

CLEANED_FILES = [
    "customers_cleaned.csv",
    "orders_cleaned.csv",
    "order_items_cleaned.csv",
    "products_cleaned.csv",
    "payments_cleaned.csv",
    "reviews_cleaned.csv",
    "sellers_cleaned.csv",
    "geolocation_cleaned.csv",
    "translations_cleaned.csv",
]


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def regenerated(project_root, tmp_path_factory):
    raw = project_root / "data" / "raw"
    if not (raw / "olist_orders_dataset.csv").exists():
        pytest.skip("data/raw does not contain the Olist CSVs")
    out = tmp_path_factory.mktemp("cleaned")
    subprocess.run(
        [sys.executable, str(project_root / "scripts" / "clean_data.py"), "--output-dir", str(out)],
        check=True,
        cwd=project_root,
    )
    return out


@pytest.mark.parametrize("filename", CLEANED_FILES)
def test_regenerated_csv_is_byte_identical_to_the_committed_pipeline_input(
    regenerated, project_root, filename
):
    existing = project_root / "data" / "processed" / filename
    if not existing.exists():
        pytest.skip(f"{existing} not present")
    assert _sha256(regenerated / filename) == _sha256(existing), filename


def test_cleaned_output_matches_the_live_database(regenerated, project_root):
    """Row counts, primary-key hashes, numeric aggregates and distinct-value hashes."""
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "verify_clean_data.py"),
            "--scratch-dir",
            str(regenerated),
            "--skip-regenerate",
        ],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OVERALL: MATCH" in result.stdout
