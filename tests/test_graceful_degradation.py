"""One leg down must degrade, not 500 (M4 Task 4).

Each test takes one dependency out at the query boundary -- the same exception the
resilience wrapper raises when a real dependency times out or its breaker is open --
runs the real retrieval against the other two live dependencies, and asserts three
things: the call completed, the surviving legs returned records, and the response says
which leg was unavailable and why.

The third assertion is the one that matters. Degrading silently is how a system
reports a confident answer built on two thirds of its evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.requires_stack


@pytest.fixture(autouse=True)
def clean_breakers():
    from resilience import reset_all_breakers

    reset_all_breakers()
    yield
    reset_all_breakers()


@pytest.fixture(scope="module")
def stack(project_root):
    load_dotenv(project_root / ".env")

    import hybrid_retriever

    try:
        settings = hybrid_retriever.load_settings()
        engine = hybrid_retriever.build_postgres_engine(settings)

        with engine.connect():
            pass

        engine.dispose()

        driver = hybrid_retriever.build_neo4j_driver(settings)
        driver.verify_connectivity()
        driver.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"stack not reachable: {exc}")

    return hybrid_retriever


ROUTE_PLAN = {
    "sql_intent": "seller_performance",
    "graph_intent": "seller_ticket_product_paths",
    "vector_artifact_groups": ["support_tickets"],
    "sql_plan": {"intent": "seller_performance", "filters": {}, "sort_by": "late_delivery_rate"},
    "graph_plan": {"intent": "seller_ticket_product_paths", "filters": {}},
    "answerable": True,
}

QUERY = "Which sellers have the worst delivery reliability?"


def run(stack, tmp_path):
    return stack.run_hybrid_retrieval(
        query=QUERY,
        output_path=Path(tmp_path) / "raw.json",
        sql_limit=10,
        graph_limit=10,
        vector_limit=5,
        route_plan=ROUTE_PLAN,
        evidence_linked_filtering=False,
    )


def assert_degraded(report, down_source, down_dependency):
    summary = report["summary"]
    legs = report["retrieval_results"]

    unavailable = summary.get("unavailable_legs") or []
    named = {leg["source"] for leg in unavailable}

    assert down_source in named, (
        f"{down_source} went down but the report does not name it as unavailable: "
        f"{unavailable}"
    )

    entry = next(leg for leg in unavailable if leg["source"] == down_source)
    assert entry["dependency"] == down_dependency
    assert entry["reason"], "unavailability must carry a reason, not just a flag"

    survivors = [
        name for name in ("sql", "graph", "vector")
        if legs[name]["source"] != down_source
    ]
    total = sum(legs[name]["record_count"] for name in survivors)

    assert total > 0, f"no surviving leg returned records: {survivors}"

    quality = summary.get("evidence_quality") or {}
    assert quality.get("confidence") == "degraded"
    assert any(down_source in reason for reason in quality.get("reasons", []))


def test_postgres_down_degrades_to_graph_and_vector(stack, tmp_path, monkeypatch):
    from resilience import DependencyUnavailable

    def unavailable(*_args, **_kwargs):
        raise DependencyUnavailable("postgres", "connection refused (simulated)")

    monkeypatch.setattr(stack, "records_query", unavailable)

    report = run(stack, tmp_path)

    assert_degraded(report, "postgresql", "postgres")
    assert report["retrieval_results"]["graph"]["record_count"] > 0
    assert report["retrieval_results"]["vector"]["record_count"] > 0


def test_neo4j_down_degrades_to_sql_and_vector(stack, tmp_path, monkeypatch):
    from resilience import DependencyUnavailable

    def unavailable(*_args, **_kwargs):
        raise DependencyUnavailable("neo4j", "connection refused (simulated)")

    monkeypatch.setattr(stack, "graph_query", unavailable)

    report = run(stack, tmp_path)

    assert_degraded(report, "neo4j", "neo4j")
    assert report["retrieval_results"]["sql"]["record_count"] > 0
    assert report["retrieval_results"]["vector"]["record_count"] > 0


def test_qdrant_down_degrades_to_sql_and_graph(stack, tmp_path, monkeypatch):
    from resilience import DependencyUnavailable

    def unavailable(*_args, **_kwargs):
        raise DependencyUnavailable("qdrant", "connection refused (simulated)")

    monkeypatch.setattr(stack, "qdrant_query_points", unavailable)

    report = run(stack, tmp_path)

    assert_degraded(report, "qdrant", "qdrant")
    assert report["retrieval_results"]["sql"]["record_count"] > 0
    assert report["retrieval_results"]["graph"]["record_count"] > 0


def test_an_open_breaker_degrades_the_leg_without_calling_it(stack, tmp_path):
    """The breaker path, not just the timeout path."""
    from resilience import breaker_for

    breaker = breaker_for("neo4j")

    for _ in range(breaker.profile.failure_threshold):
        breaker.record_failure()

    assert breaker.state == "open"

    report = run(stack, tmp_path)

    assert_degraded(report, "neo4j", "neo4j")
    assert "circuit breaker is open" in json.dumps(report["summary"]["unavailable_legs"])


def test_the_degradation_reaches_the_answer_prompt():
    """A note the answer agent cannot see is not a note."""
    from retrieval_context_builder import build_context_text

    context = {
        "business_question": "Which sellers have the worst delivery reliability?",
        "source_summary": {
            "sql": {"intent": "seller_performance", "records": 10, "skipped": False,
                    "filters": {}, "sort_by": "late_delivery_rate"},
            "graph": {"intent": None, "records": 0, "skipped": False, "filters": {}},
            "vector": {"artifact_groups": ["support_tickets"], "records": 5},
        },
        "unavailable_legs": [
            {"source": "neo4j", "dependency": "neo4j",
             "reason": "neo4j circuit breaker is open after 3 consecutive failures"},
        ],
        "evidence_quality": {"confidence": "degraded", "reasons": ["neo4j was unavailable"]},
        "recommended_next_actions": [],
        "entity_ids": {},
    }

    text = build_context_text(context)

    assert "DEGRADED RETRIEVAL" in text
    assert "neo4j" in text
    assert "circuit breaker is open" in text
    assert "based on the remaining sources only" in text


def test_all_three_legs_down_is_not_a_silent_empty_answer(stack, tmp_path, monkeypatch):
    """Total outage must still be a stated failure, not a confident empty answer."""
    from resilience import DependencyUnavailable

    for name, dependency in (
        ("records_query", "postgres"),
        ("graph_query", "neo4j"),
        ("qdrant_query_points", "qdrant"),
    ):
        def unavailable(*_a, _d=dependency, **_k):
            raise DependencyUnavailable(_d, "connection refused (simulated)")

        monkeypatch.setattr(stack, name, unavailable)

    report = run(stack, tmp_path)

    unavailable_legs = report["summary"]["unavailable_legs"]

    assert len(unavailable_legs) == 3
    assert report["summary"]["evidence_quality"]["confidence"] == "degraded"

    total = sum(
        report["retrieval_results"][name]["record_count"]
        for name in ("sql", "graph", "vector")
    )
    assert total == 0
