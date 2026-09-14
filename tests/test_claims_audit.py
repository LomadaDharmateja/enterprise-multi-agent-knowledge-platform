"""The M9 exit criterion, in the suite (M9 Task 4).

    **Exit:** zero contradicted claims, machine-checked.

`scripts/audit_claims.py` is the machine. This puts its verdict in the test suite so a
future commit that makes a documented claim false fails the build rather than waiting
for somebody to re-read the README.

It reads `audit/results.json` rather than re-running the audit: the full audit runs the
test suite as one of its checks, so invoking it from inside the suite would recurse.
The staleness of that file is itself asserted below -- a green result from six months
ago is not evidence about today.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS = PROJECT_ROOT / "audit" / "results.json"
MANIFEST = PROJECT_ROOT / "audit" / "claims.json"

# The eleven claims AUDIT.md found contradicted on 2026-08-20. The rebuild exists
# because of these; none may regress.
ORIGINALLY_CONTRADICTED = {29, 30, 34, 38, 42, 49, 58, 61, 64, 82, 104}

MAX_RESULT_AGE = timedelta(days=30)


@pytest.fixture(scope="module")
def results():
    if not RESULTS.exists():
        pytest.skip("audit/results.json is absent -- run scripts/audit_claims.py")

    return json.loads(RESULTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_no_claim_is_contradicted(results):
    """The M9 exit criterion."""
    contradicted = [
        f"#{r['id']} {r['claim'][:70]} -- {str(r['evidence'])[:110]}"
        for r in results["results"]
        if r["verdict"] == "CONTRADICTED"
    ]

    assert not contradicted, "contradicted claims:\n  " + "\n  ".join(contradicted)


def test_no_check_errored(results):
    """A check that crashed is not a claim that holds.

    Without this, deleting a table would turn a CONTRADICTED claim into an ERROR and
    the exit criterion would go green on a broken system.
    """
    errored = [
        f"#{r['id']} {str(r['evidence'])[:110]}"
        for r in results["results"]
        if r["verdict"] == "ERROR"
    ]

    assert not errored, "checks that crashed:\n  " + "\n  ".join(errored)


def test_none_of_the_original_eleven_regressed(results):
    """The specific claims the rebuild was commissioned to fix."""
    by_id = {r["id"]: r for r in results["results"]}
    missing = ORIGINALLY_CONTRADICTED - set(by_id)

    assert not missing, f"claims dropped from the manifest entirely: {sorted(missing)}"

    regressed = {
        claim_id: by_id[claim_id]["verdict"]
        for claim_id in sorted(ORIGINALLY_CONTRADICTED)
        if by_id[claim_id]["verdict"] == "CONTRADICTED"
    }

    assert not regressed, f"originally-contradicted claims that are false again: {regressed}"


def test_zero_contradicted_was_not_achieved_by_deletion(results, manifest):
    """The number is meaningless if inconvenient claims simply left the manifest.

    NOT_CLAIMED is a legitimate verdict -- the current documentation genuinely does
    not assert some things the old one did -- but it must stay rare and each one must
    carry a written reason.
    """
    assert len(manifest["claims"]) >= 110, (
        f"the manifest has shrunk to {len(manifest['claims'])}; the audit covers the "
        f"original 110 claims plus whatever the current documentation adds"
    )

    not_claimed = [r for r in results["results"] if r["verdict"] == "NOT_CLAIMED"]

    assert len(not_claimed) <= 6, (
        f"{len(not_claimed)} claims are NOT_CLAIMED. Dropping claims is how a "
        f"contradicted-claim count reaches zero without anything being fixed."
    )

    unexplained = [r["id"] for r in not_claimed if len(str(r.get("evidence", ""))) < 40]

    assert not unexplained, f"NOT_CLAIMED without a stated reason: {unexplained}"


def test_superseded_limitations_are_not_repeated_in_the_readme(results):
    """Eleven claims were true in August and are false now because they were fixed.

    "No caching", "no replanning", "no cloud deployment" and their siblings were
    honest limitations. Repeating them in the current README would be a new false
    claim -- the failure mode the rebuild exists to remove, arrived at from the
    opposite direction.
    """
    superseded = [r for r in results["results"] if r["verdict"] == "SUPERSEDED"]

    assert superseded, "no SUPERSEDED verdicts -- the audit is not distinguishing " \
                       "'still true' from 'fixed since'"

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8").lower()

    repeated = [
        f"#{r['id']} {r['claim']}"
        for r in superseded
        if r["claim"].lower().startswith("no ") and r["claim"].lower() in readme
    ]

    assert not repeated, f"the README repeats a limitation that has been fixed: {repeated}"


def test_the_audit_result_is_not_stale(results):
    """A green verdict from months ago says nothing about today."""
    generated = datetime.strptime(results["generated_at"], "%Y-%m-%dT%H:%M:%S%z")
    age = datetime.now(timezone.utc) - generated.astimezone(timezone.utc)

    assert age < MAX_RESULT_AGE, (
        f"audit/results.json is {age.days} days old. Re-run "
        f"`python scripts/audit_claims.py`."
    )


def test_the_coverage_figure_is_reported_honestly(manifest):
    """Claims with no executable check must be counted, not hidden."""
    coverage = manifest["coverage"]
    inherited = [c for c in manifest["claims"] if c.get("inherited")]

    assert coverage["inherited_unchecked"] == len(inherited), (
        "the manifest's own coverage count disagrees with the claims in it"
    )

    assert coverage["executable"] >= 80, (
        f"only {coverage['executable']} claims have an executable check; the audit "
        f"is drifting back towards being a document people read"
    )
