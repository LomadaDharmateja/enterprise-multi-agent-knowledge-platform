"""Executable checks for the claim audit (M9).

The first audit was a person reading code and running commands. Its conclusions were
right, but they were not repeatable: nothing stopped the same claims drifting out of
true again the next week. The M9 exit criterion says "zero contradicted claims,
**machine-checked**", so every claim that can be settled by running something is
settled by running something, and the result is a number rather than a reading.

A check returns (verdict, evidence). Verdicts:

    CONFIRMED    the check ran and the claim holds
    CAVEAT       holds, with a limitation that must appear in the README
    CONTRADICTED the check ran and the claim is false      <- must be zero
    SUPERSEDED   true at the first audit, false now because the gap was closed.
                 M2-M8 built caching, replanning, circuit breakers, cost accounting,
                 a calibrated evaluator and a deployment -- so "no caching", "no
                 replanning" and their nine siblings became false by being fixed.
                 Republishing them as CONFIRMED would be a new false claim.
    NOT_CLAIMED  the current documentation does not assert it. Must be justified;
                 this is not a way to make an inconvenient claim disappear.
    UNVERIFIABLE cannot be settled by running anything. Must be stated as such.

Nothing here writes. Every database check uses a read-only path.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]

for _d in ("retrieval", "observability", "planning", "orchestration", "api"):
    _p = PROJECT_ROOT / "src" / _d
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

CONFIRMED = "CONFIRMED"
CAVEAT = "CAVEAT"
CONTRADICTED = "CONTRADICTED"
SUPERSEDED = "SUPERSEDED"
NOT_CLAIMED = "NOT_CLAIMED"
UNVERIFIABLE = "UNVERIFIABLE"

REGISTRY: dict[str, Callable[..., tuple[str, str]]] = {}


def check(name: str):
    def register(fn):
        REGISTRY[name] = fn
        return fn
    return register


# --------------------------------------------------------------------------------
# Connections. Read-only, and cached so 110 claims do not open 110 connections.
# --------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")

    from hybrid_retriever import load_settings

    return load_settings()


@lru_cache(maxsize=1)
def pg():
    from sqlalchemy import create_engine

    s = settings()
    url = (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER')}:"
        f"{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_HOST', 'localhost')}:"
        f"{os.getenv('POSTGRES_PORT', '5432')}/{os.getenv('POSTGRES_DB')}"
    )
    return create_engine(url, execution_options={"postgresql_readonly": True})


def sql_scalar(query: str) -> Any:
    from sqlalchemy import text

    with pg().connect() as conn:
        return conn.execute(text(query)).scalar()


def sql_rows(query: str) -> list[tuple]:
    from sqlalchemy import text

    with pg().connect() as conn:
        return [tuple(r) for r in conn.execute(text(query))]


@lru_cache(maxsize=1)
def neo4j_driver():
    from hybrid_retriever import build_neo4j_driver

    return build_neo4j_driver(settings())


def cypher(query: str) -> list[dict]:
    driver = neo4j_driver()

    with driver.session(default_access_mode="READ") as session:
        return [dict(r) for r in session.run(query)]


@lru_cache(maxsize=1)
def qdrant():
    from hybrid_retriever import build_qdrant_client

    return build_qdrant_client(settings())


@lru_cache(maxsize=1)
def qdrant_sample(limit: int = 400) -> list[Any]:
    points, _ = qdrant().scroll(
        collection_name=settings()["qdrant_collection"],
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return points


def source(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------------------
# Generic checks, parameterised from the manifest
# --------------------------------------------------------------------------------


@check("sql_count")
def sql_count(query: str, expected: int, **_) -> tuple[str, str]:
    actual = sql_scalar(query)
    verdict = CONFIRMED if int(actual) == int(expected) else CONTRADICTED
    return verdict, f"{query.strip()} -> {actual} (expected {expected})"


@check("sql_at_least")
def sql_at_least(query: str, minimum: int, **_) -> tuple[str, str]:
    actual = sql_scalar(query)
    verdict = CONFIRMED if int(actual) >= int(minimum) else CONTRADICTED
    return verdict, f"{query.strip()} -> {actual} (>= {minimum})"


@check("cypher_count")
def cypher_count(query: str, expected: int, **_) -> tuple[str, str]:
    rows = cypher(query)
    actual = list(rows[0].values())[0]
    verdict = CONFIRMED if int(actual) == int(expected) else CONTRADICTED
    return verdict, f"{query.strip()} -> {actual} (expected {expected})"


@check("cypher_at_least")
def cypher_at_least(query: str, minimum: int, **_) -> tuple[str, str]:
    rows = cypher(query)
    actual = list(rows[0].values())[0]
    verdict = CONFIRMED if int(actual) >= int(minimum) else CONTRADICTED
    return verdict, f"{query.strip()} -> {actual} (>= {minimum})"


@check("qdrant_points")
def qdrant_points(expected: int, **_) -> tuple[str, str]:
    collection = settings()["qdrant_collection"]
    actual = qdrant().get_collection(collection).points_count
    verdict = CONFIRMED if actual == expected else CONTRADICTED
    return verdict, f"collection {collection!r} points_count={actual} (expected {expected})"


@check("file_exists")
def file_exists(paths: list[str], **_) -> tuple[str, str]:
    missing = [p for p in paths if not (PROJECT_ROOT / p).exists()]
    verdict = CONFIRMED if not missing else CONTRADICTED
    return verdict, f"present: {len(paths) - len(missing)}/{len(paths)}" + (
        f"; missing {missing}" if missing else ""
    )


@check("source_contains")
def source_contains(path: str, patterns: list[str], **_) -> tuple[str, str]:
    text = source(path)
    missing = [p for p in patterns if not re.search(p, text)]
    verdict = CONFIRMED if not missing else CONTRADICTED
    return verdict, f"{path}: matched {len(patterns) - len(missing)}/{len(patterns)}" + (
        f"; missing {missing}" if missing else ""
    )


@check("source_absent")
def source_absent(path: str, patterns: list[str], **_) -> tuple[str, str]:
    text = source(path)
    present = [p for p in patterns if re.search(p, text)]
    verdict = CONFIRMED if not present else CONTRADICTED
    return verdict, f"{path}: none of {patterns} present" if not present else (
        f"{path}: unexpectedly present {present}"
    )


@check("pytest_passes")
def pytest_passes(tests: list[str], **_) -> tuple[str, str]:
    """A named test is the claim's proof. If it fails, the claim fails."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", *tests],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=900,
    )
    tail = [l for l in result.stdout.strip().splitlines() if l.strip()][-1:] or [""]
    verdict = CONFIRMED if result.returncode == 0 else CONTRADICTED
    return verdict, f"{' '.join(tests)} -> {tail[0]}"


@check("manual")
def manual(verdict: str, evidence: str, **_) -> tuple[str, str]:
    """A verdict that cannot be derived by running something.

    Used for UNVERIFIABLE claims about who did the work, and for NOT_CLAIMED /
    SUPERSEDED judgements. The runner reports how many of these there are, because a
    manifest that quietly became all-manual would defeat the point.
    """
    return verdict, evidence


# --------------------------------------------------------------------------------
# Claim-specific checks -- the eleven that were contradicted, and their neighbours
# --------------------------------------------------------------------------------

ENTITY_ID_KEYS = ("seller_id", "seller_ids", "product_id", "product_ids",
                  "order_id", "order_ids", "customer_id", "customer_ids")


@check("qdrant_entity_ids_present")
def qdrant_entity_ids_present(min_fraction: float = 0.5, **_) -> tuple[str, str]:
    """Claims 29 and 34. F-01: 0 of 8,152 points carried an Olist entity ID."""
    points = qdrant_sample()
    with_ids = 0

    for point in points:
        entity_ids = (point.payload or {}).get("entity_ids") or {}
        if any(entity_ids.get(k) for k in ENTITY_ID_KEYS):
            with_ids += 1

    fraction = with_ids / len(points) if points else 0.0
    verdict = CONFIRMED if fraction >= min_fraction else CONTRADICTED

    return verdict, (
        f"{with_ids}/{len(points)} sampled points carry an Olist entity id "
        f"({fraction:.1%}); first audit measured 0/8152"
    )


@check("qdrant_group_has_entity_ids")
def qdrant_group_has_entity_ids(group: str, keys: list[str], **_) -> tuple[str, str]:
    """Claim 30: a warranty claim must reference a product id and a seller id."""
    from qdrant_client import models

    points, _ = qdrant().scroll(
        collection_name=settings()["qdrant_collection"],
        scroll_filter=models.Filter(must=[
            models.FieldCondition(key="artifact_group",
                                  match=models.MatchValue(value=group))
        ]),
        limit=200, with_payload=True, with_vectors=False,
    )

    if not points:
        return CONTRADICTED, f"no points in artifact_group={group!r}"

    hits = {k: 0 for k in keys}

    for point in points:
        entity_ids = (point.payload or {}).get("entity_ids") or {}
        for k in keys:
            if entity_ids.get(k):
                hits[k] += 1

    missing = [k for k, n in hits.items() if n == 0]
    verdict = CONFIRMED if not missing else CONTRADICTED

    return verdict, (
        f"{group}: n={len(points)}; "
        + ", ".join(f"{k} on {n}" for k, n in hits.items())
        + (f"; ABSENT {missing}" if missing else "")
    )


@check("entity_id_joins_to_postgres")
def entity_id_joins_to_postgres(**_) -> tuple[str, str]:
    """The seam claims 29/30 actually rest on: an id from Qdrant must find a row."""
    points = qdrant_sample()
    seller_ids: list[str] = []

    for point in points:
        raw = ((point.payload or {}).get("entity_ids") or {}).get("seller_ids") or []
        seller_ids += [s for s in (raw if isinstance(raw, list) else [raw]) if s]

    if not seller_ids:
        return CONTRADICTED, "no seller ids in the sampled Qdrant payloads to join with"

    sample = sorted(set(seller_ids))[:25]
    placeholders = ", ".join(f"'{s}'" for s in sample if re.fullmatch(r"[0-9a-f]{32}", s))

    if not placeholders:
        return CONTRADICTED, f"seller ids are not Olist-shaped: {sample[:3]}"

    found = sql_scalar(
        f"SELECT count(*) FROM ecommerce.sellers WHERE seller_id IN ({placeholders})"
    )
    total = len(placeholders.split(","))
    verdict = CONFIRMED if found == total else CONTRADICTED

    return verdict, f"{found}/{total} seller ids taken from Qdrant payloads resolve in PostgreSQL"


@check("sql_only_plan_is_representable")
def sql_only_plan_is_representable(**_) -> tuple[str, str]:
    """Claims 38, 42 and 49. A plan naming one store used to be unrepresentable:
    "Which seller had the highest revenue?" raised
    `ValueError: Invalid graph intent from Gemini: None`."""
    from gemini_query_planner import validate_and_normalize_plan

    plan = validate_and_normalize_plan({
        "answerable": True,
        "sql_intent": "seller_performance",
        "graph_intent": None,
        "vector_artifact_groups": [],
    })

    if plan.get("sql_intent") != "seller_performance":
        return CONTRADICTED, f"sql intent lost in validation: {plan.get('sql_intent')!r}"

    if plan.get("graph_intent") is not None:
        return CONTRADICTED, f"a null graph intent was rewritten to {plan['graph_intent']!r}"

    return CONFIRMED, (
        "validate_and_normalize_plan accepts sql_intent with graph_intent=None and "
        "vector_artifact_groups=[] -- a single-store plan is representable"
    )


@check("legs_can_be_skipped")
def legs_can_be_skipped(**_) -> tuple[str, str]:
    """Claims 38 and 42: the three legs must be conditional, not unconditional."""
    tree = ast.parse(source("src/retrieval/hybrid_retriever.py"))
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "run_hybrid_retrieval"),
        None,
    )

    if fn is None:
        return CONTRADICTED, "run_hybrid_retrieval not found"

    markers = [
        "skipped_leg_result" in ast.dump(fn),
        "forced_sql_intent is None" in source("src/retrieval/hybrid_retriever.py"),
        "forced_vector_groups == []" in source("src/retrieval/hybrid_retriever.py"),
    ]

    verdict = CONFIRMED if all(markers) else CONTRADICTED

    return verdict, (
        "run_hybrid_retrieval guards each leg on the plan "
        f"(skipped_leg_result + per-leg None/[] guards present: {sum(markers)}/3)"
    )


@check("health_reports_real_dependencies")
def health_reports_real_dependencies(**_) -> tuple[str, str]:
    """Claim 61. F-07: /health was a static literal and returned ok while every
    dependency was unreachable."""
    text = source("src/api/main.py")

    if re.search(r'"status":\s*"ok"[^\n]*\n\s*\}', text) and "DEPENDENCY_CHECKS" not in text:
        return CONTRADICTED, "/health still returns a literal"

    probes = re.findall(r"def (_check_\w+)", text)
    has_503 = "HTTP_503_SERVICE_UNAVAILABLE" in text

    verdict = CONFIRMED if len(probes) >= 3 and has_503 else CONTRADICTED

    return verdict, (
        f"/health probes {probes} and returns 503 when any fails: {has_503}"
    )


@check("runtime_is_read_only")
def runtime_is_read_only(**_) -> tuple[str, str]:
    """Claim 82. The first audit wrote to every store with the runtime's own helpers."""
    role = os.getenv("POSTGRES_READONLY_USER")

    if not role:
        return CONTRADICTED, "POSTGRES_READONLY_USER is not configured"

    is_superuser = sql_scalar(
        f"SELECT rolsuper FROM pg_roles WHERE rolname = '{role}'"
    )

    if is_superuser is None:
        return CONTRADICTED, f"role {role!r} does not exist in pg_roles"

    if is_superuser:
        return CONTRADICTED, f"role {role!r} is a superuser"

    can_write = sql_scalar(
        f"SELECT has_table_privilege('{role}', 'ecommerce.sellers', 'INSERT')"
    )
    can_read = sql_scalar(
        f"SELECT has_table_privilege('{role}', 'ecommerce.sellers', 'SELECT')"
    )

    retriever = source("src/retrieval/hybrid_retriever.py")
    readonly_tx = "postgresql_readonly" in retriever
    read_session = "execute_read" in retriever or 'default_access_mode="READ"' in retriever

    ok = (not can_write) and can_read and readonly_tx and read_session
    verdict = CONFIRMED if ok else CONTRADICTED

    return verdict, (
        f"role {role!r}: superuser={is_superuser}, SELECT={can_read}, INSERT={can_write}; "
        f"read-only transactions={readonly_tx}, read-mode Neo4j sessions={read_session}"
    )


@check("qdrant_requires_a_key")
def qdrant_requires_a_key(**_) -> tuple[str, str]:
    """Part of claim 82. The first audit upserted into Qdrant with no credential."""
    import urllib.error
    import urllib.request

    host = os.getenv("QDRANT_HOST", "localhost")
    port = os.getenv("QDRANT_HTTP_PORT", "6333")
    url = f"http://{host}:{port}/collections"

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return CONTRADICTED, (
                f"unauthenticated GET {url} returned {response.status} -- Qdrant is open"
            )
    except urllib.error.HTTPError as exc:
        verdict = CONFIRMED if exc.code in (401, 403) else CONTRADICTED
        return verdict, f"unauthenticated GET {url} -> HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return CONTRADICTED, f"could not probe Qdrant: {type(exc).__name__}: {exc}"


@check("deterministic_checks_can_fail_a_run")
def deterministic_checks_can_fail_a_run(**_) -> tuple[str, str]:
    """Claim 58. The five checks were keyword/length heuristics; four were
    severity: medium and could not cause a FAIL."""
    path = PROJECT_ROOT / "src" / "evaluation" / "gemini_answer_evaluator.py"

    if not path.exists():
        return CONTRADICTED, "gemini_answer_evaluator.py not found"

    text = path.read_text(encoding="utf-8")
    high = len(re.findall(r'"severity":\s*"high"', text))
    medium = len(re.findall(r'"severity":\s*"medium"', text))

    verdict = CONFIRMED if high >= 1 else CONTRADICTED

    return verdict, (
        f"deterministic checks: {high} at severity=high (can fail a run), "
        f"{medium} at medium (advisory). First audit: 0 high, only len>=1000 decisive"
    )


@check("test_suite_passes")
def test_suite_passes(minimum: int, deterministic_only: bool = False, **_):
    """Claim 104's replacement. "Fully validated" is not a measurable claim; "N tests
    pass" is.

    `deterministic_only` deselects the four tests that make a live Gemini call. They
    are not flaky by accident: the planner is an LLM with no pinned temperature, and
    M7 measured `graph_intent` reproducing on only 10 of 14 replays. Measured here at
    **1 failure in 9 full runs**, always
    `test_revenue_question_produces_a_representable_sql_only_plan`.

    A gate that goes red one run in nine for reasons outside the code teaches people
    to re-run it until it passes, which is worse than not having it. So the gate is
    the deterministic subset, and the non-deterministic four are measured and
    reported rather than asserted.
    """
    command = [sys.executable, "-m", "pytest", "-q", "--no-header"]

    if deterministic_only:
        command += ["-m", "not requires_gemini"]

    result = subprocess.run(
        command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3600,
    )

    match = re.search(r"(\d+) passed", result.stdout)
    passed = int(match.group(1)) if match else 0
    failed = re.search(r"(\d+) failed", result.stdout)

    verdict = CONFIRMED if (result.returncode == 0 and passed >= minimum) else CONTRADICTED

    return verdict, (
        f"{passed} passed"
        + (f", {failed.group(1)} FAILED" if failed else "")
        + f" (claim requires >= {minimum}, exit {result.returncode}"
        + (", live-LLM tests deselected" if deterministic_only else "")
        + ")"
    )


@check("feature_now_exists")
def feature_now_exists(paths: list[str], patterns: list[str], milestone: str, **_):
    """For the negative claims -- "no caching", "no replanning" and their siblings.

    True at the first audit. False now, because M2-M8 built the thing. The verdict is
    SUPERSEDED rather than CONTRADICTED: the original claim was honest when it was
    made, and the job now is to make sure the *current* documentation does not repeat
    it.
    """
    found = []

    for path in paths:
        full = PROJECT_ROOT / path

        if not full.exists():
            continue

        text = full.read_text(encoding="utf-8")
        found += [p for p in patterns if re.search(p, text)]

    if found:
        return SUPERSEDED, (
            f"{milestone} implemented this; the limitation no longer holds "
            f"(evidence: {sorted(set(found))[:3]} in {paths[0]})"
        )

    return CONFIRMED, f"still absent: none of {patterns} found in {paths}"


@check("qdrant_vector_dim")
def qdrant_vector_dim(expected: int, **_) -> tuple[str, str]:
    """Claim 32. Read from the live collection, not from a constant in a file."""
    info = qdrant().get_collection(settings()["qdrant_collection"])
    params = info.config.params

    size = getattr(getattr(params, "vectors", None), "size", None)

    if size is None and isinstance(getattr(params, "vectors", None), dict):
        size = next(iter(params.vectors.values())).size

    verdict = CONFIRMED if size == expected else CONTRADICTED
    return verdict, f"live collection vector size = {size} (expected {expected})"


@check("qdrant_composition")
def qdrant_composition(expected: dict, retired_total: int | None = None, **_):
    """The corpus is what the design says it is, per artifact group.

    Written after this audit's own first run reported the corpus 2,054 documents
    short. It was not short: docs/CORPUS_DESIGN.md retired the 8,152 figure during M1
    and the corpus is deliberately 6,098. The manifest had been checking the number
    from the document under audit instead of the number from the current design --
    the exact mistake the audit exists to catch, made by the audit.

    Checking composition per group, rather than a single total, means a future drift
    in one group cannot hide inside a correct-looking total.
    """
    import collections

    counts: collections.Counter = collections.Counter()
    offset = None

    while True:
        points, offset = qdrant().scroll(
            collection_name=settings()["qdrant_collection"],
            limit=1000, offset=offset, with_payload=True, with_vectors=False,
        )

        for point in points:
            counts[(point.payload or {}).get("artifact_group", "?")] += 1

        if offset is None:
            break

    deltas = {g: counts.get(g, 0) - n for g, n in expected.items()}
    wrong = {g: d for g, d in deltas.items() if d}
    extra = set(counts) - set(expected)

    detail = ", ".join(f"{g}={counts.get(g, 0)}" for g in sorted(expected))
    total = sum(counts.values())

    if wrong or extra:
        return CONTRADICTED, (
            f"composition drift {wrong}"
            + (f"; undocumented groups {sorted(extra)}" if extra else "")
            + f"; total {total}"
        )

    note = ""

    if retired_total is not None:
        note = (
            f". The first audit's {retired_total:,} was retired in "
            f"docs/CORPUS_DESIGN.md during M1; the reduction is intentional"
        )

    return CONFIRMED, f"total {total}: {detail}{note}"


@check("consistent_number")
def consistent_number(value: str, files: list[str], **_) -> tuple[str, str]:
    """A figure quoted in the README must match the artefact that measured it.

    This is the drift check. The audit's own origin story is a number that was
    measured once, republished, and quietly stopped being true; a README that states
    `$0.2972` while docs/M5_COST_TABLE.md says something else is that failure
    starting again. The literal has to appear in every file listed -- the claim and
    its source -- so they cannot separate.
    """
    missing = []

    for relative in files:
        path = PROJECT_ROOT / relative

        if not path.exists():
            missing.append(f"{relative} (absent)")
            continue

        if value not in path.read_text(encoding="utf-8"):
            missing.append(relative)

    verdict = CONFIRMED if not missing else CONTRADICTED

    return verdict, (
        f"{value!r} present in all of {files}" if not missing
        else f"{value!r} MISSING from {missing} -- the README and its source disagree"
    )


@check("url_responds")
def url_responds(url: str, expect_status: int = 200, contains: str | None = None, **_):
    """The README says the demo is live. Ask it.

    A network failure is reported UNVERIFIABLE rather than CONTRADICTED -- a laptop
    with no connection is not evidence that the deployment is down. A reachable
    service answering the wrong thing is CONTRADICTED.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            status = response.status
            body = response.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return CONTRADICTED, f"GET {url} -> HTTP {exc.code} (expected {expect_status})"
    except Exception as exc:  # noqa: BLE001
        return UNVERIFIABLE, (
            f"GET {url} could not be attempted: {type(exc).__name__}: {exc}. "
            f"Not evidence that the deployment is down."
        )

    if status != expect_status:
        return CONTRADICTED, f"GET {url} -> HTTP {status} (expected {expect_status})"

    if contains and contains not in body:
        # A JSON body may be served compact (`"mode":"demo"`) or pretty
        # (`"mode": "demo"`). Re-serialising with canonical separators means the
        # expectation is about the data, not about the server's whitespace -- the
        # first version of this check reported the live demo CONTRADICTED over a
        # missing space.
        matched = False

        try:
            matched = contains in json.dumps(json.loads(body))
        except ValueError:
            matched = False

        if not matched:
            return CONTRADICTED, f"GET {url} -> {status} but body lacks {contains!r}"

    return CONFIRMED, f"GET {url} -> HTTP {status}" + (
        f", body contains {contains!r}" if contains else ""
    )


@check("collected_test_count")
def collected_test_count(total: int, deterministic: int, files: list[str], **_):
    """The README's test count must match what pytest collects, not merely exceed it.

    `test_suite_passes` asserts a floor, so adding tests keeps it green while the
    README's number silently goes stale -- which happened during M9 itself: the README
    said 300 while the suite had grown to 316. A floor is the right gate for "the
    suite is green"; it is the wrong check for "this number is true".
    """
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "--collect-only"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=900,
    ).stdout

    match = re.search(r"(\d+) tests collected", collected)
    actual = int(match.group(1)) if match else -1

    problems = []

    if actual != total:
        problems.append(f"pytest collects {actual}, the claim says {total}")

    for relative in files:
        text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")

        for number, label in ((total, "total"), (deterministic, "deterministic")):
            if f"{number}" not in text:
                problems.append(f"{relative} does not state the {label} count {number}")

    verdict = CONFIRMED if not problems else CONTRADICTED

    return verdict, (
        f"pytest collects {actual}; README states {total} total / {deterministic} "
        f"deterministic" if not problems else "; ".join(problems)
    )
