"""The M1 exit criterion, checkable with no live stack — so it runs in CI.

tests/test_corpus_signal.py is the real boundary test: it reads complaint counts
from Qdrant and seller quality from PostgreSQL, so it fails if the JSONL -> Qdrant
handoff loses the entity linkage. But it is marked requires_stack, and the CI
runner has no stack, so there it *skips*.

A test that always skips is not a test that runs. Shipping one as the M1 exit
criterion would repeat the exact finding the audit made about this project:
validators that reported PASS while they could not fail. So the live test's inputs
are snapshotted into tests/baseline/corpus_signal_sample.json by
scripts/export_corpus_signal_sample.py, and this module recomputes the correlation
from that fixture with no dependencies beyond scipy.

The division of labour:
  test_corpus_signal.py     proves the linkage survives the JSONL -> Qdrant seam.
                            Runs where a stack exists. The stronger test.
  test_corpus_signal_ci.py  proves the committed corpus still carries the required
                            signal. Runs everywhere, including CI. Goes red on a
                            regression even though it cannot see the seam.

Regenerate the fixture whenever the corpus is regenerated, or this test is checking
a corpus that no longer exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = PROJECT_ROOT / "tests" / "baseline" / "corpus_signal_sample.json"

BAND_LOW = 0.3
BAND_HIGH = 0.6

#: AUDIT.md P1, measured by drawing sellers uniformly at random.
UNIFORM_NULL_RHO = 0.017
#: AUDIT.md P1, the v1 corpus's actual correlation with order volume.
V1_VOLUME_RHO = 0.867


@pytest.fixture(scope="module")
def sample():
    if not SAMPLE_PATH.exists():
        pytest.fail(
            f"{SAMPLE_PATH.relative_to(PROJECT_ROOT)} is missing. Generate it with "
            f"`python scripts/export_corpus_signal_sample.py` against a live stack."
        )
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def _spearman(x, y):
    return pytest.importorskip("scipy.stats").spearmanr(x, y).statistic


def test_sample_fixture_is_well_formed(sample):
    assert sample["sample_size"] == len(sample["sellers"]) == 400
    assert sample["sample_seed"] == 20260824
    ids = [row["seller_id"] for row in sample["sellers"]]
    assert len(set(ids)) == len(ids), "duplicate sellers in the sample"


def test_every_ticket_point_carried_a_seller_id(sample):
    """AUDIT.md F-01: 0 of 8,152 v1 Qdrant points carried any entity ID.

    Recorded at export time from the live Qdrant payloads, so a regression that
    drops the IDs again shows up here even without a stack.
    """
    total = sample["qdrant_ticket_points"]
    with_seller = sample["qdrant_ticket_points_with_seller_id"]
    assert total > 0
    assert with_seller == total, (
        f"{total - with_seller} of {total} support-ticket points in Qdrant carried "
        f"no seller_id. This is F-01: the vector layer cannot join back to SQL."
    )


def test_complaint_counts_correlate_with_seller_quality(sample):
    """The M1 exit criterion.

    The band is two-sided on purpose. Below it the corpus carries no real signal;
    above it the corpus is recoverable from a PostgreSQL column and the vector
    layer is redundant. v1 failed on the high side at +0.867 against order volume.
    """
    complaints = [row["complaints"] for row in sample["sellers"]]
    late_rate = [row["late_rate"] for row in sample["sellers"]]

    rho = _spearman(complaints, late_rate)

    assert BAND_LOW <= abs(rho) <= BAND_HIGH, (
        f"rho(complaints, late_delivery_rate) = {rho:+.4f}, outside the required "
        f"band [{BAND_LOW}, {BAND_HIGH}].\n"
        f"  Below {BAND_LOW}: no real signal (v1's uniform null was {UNIFORM_NULL_RHO}).\n"
        f"  Above {BAND_HIGH}: recoverable from SQL, so the vector layer adds nothing "
        f"(v1 scored +{V1_VOLUME_RHO} against order volume).\n"
        f"See docs/CORPUS_DESIGN.md Part 2 section 1."
    )


def test_signal_is_quality_not_volume(sample):
    """AUDIT.md P1: v1's complaints tracked order count, not seller quality."""
    complaints = [row["complaints"] for row in sample["sellers"]]
    n_orders = [row["n_orders"] for row in sample["sellers"]]
    late_rate = [row["late_rate"] for row in sample["sellers"]]

    rho_volume = _spearman(complaints, n_orders)
    rho_quality = _spearman(complaints, late_rate)

    assert rho_volume < 0.6, (
        f"rho(complaints, n_orders) = {rho_volume:+.4f}; v1's was +{V1_VOLUME_RHO}. "
        f"Complaint volume is tracking order volume rather than seller quality."
    )
    assert abs(rho_quality) > rho_volume - 0.15, (
        f"quality correlation ({rho_quality:+.4f}) is materially weaker than the "
        f"volume correlation ({rho_volume:+.4f}); the corpus is volume-driven."
    )


def test_review_score_correlation_is_recorded(sample):
    """D-2: reported for completeness, asserted on magnitude.

    The sign is negative because mean_review is a quality measure while late_rate
    is a defect measure. Both describe the same relationship.
    """
    complaints = [row["complaints"] for row in sample["sellers"]]
    mean_review = [row["mean_review"] for row in sample["sellers"]]

    rho = _spearman(complaints, mean_review)

    assert rho < 0, f"expected complaints to fall as review score rises, got {rho:+.4f}"
    assert BAND_LOW <= abs(rho) <= BAND_HIGH, (
        f"abs(rho(complaints, mean_review)) = {abs(rho):.4f}, outside "
        f"[{BAND_LOW}, {BAND_HIGH}]"
    )


def test_complaints_land_on_well_rated_sellers_too(sample):
    """v1 put all 3,000 tickets on review_score=1 orders -- zero variance."""
    rows = sample["sellers"]
    total = sum(row["complaints"] for row in rows)
    assert total > 0

    on_well_rated = sum(
        row["complaints"] for row in rows if row["mean_review"] >= 4.0
    )
    share = on_well_rated / total
    assert 0.20 <= share <= 0.70, (
        f"{share:.1%} of sampled complaints land on sellers rated >= 4.0, outside "
        f"[20%, 70%]. A complaint must not imply a bad seller."
    )
