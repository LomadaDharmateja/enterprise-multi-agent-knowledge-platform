"""Deterministic replay and the trace endpoint (M7 Tasks 3 and 4).

Task 3 asks whether a recorded run can be replayed against the current system and land
on the same route. Two things have to hold for that to mean anything:

  1. A recorded run must be *identifiable* -- the JSONL event log has to carry enough to
     find the run and the route it produced. That is checked offline, over all 82 items
     of the M3 full run, and needs no API key.
  2. Replaying it must reproduce the route. That needs a live planner call, so it is
     marked `requires_gemini`.

Task 4 asserts GET /traces/{run_id} returns the spans for a run. The fixture is the
seven-span shape from Task 1, built in process against the memory exporter, so the test
costs nothing and does not depend on a database being up.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

os.environ.setdefault("OTEL_EXPORTER", "memory")

import replay_run  # noqa: E402

from otel import (  # noqa: E402
    agent_span,
    memory_store,
    record_llm_usage,
    retrieval_parent_span,
    retrieval_span,
    setup_tracing,
    workflow_span,
)

# The M3 headline run: 82 items, the one docs/M3_RESULTS.md reports.
M3_RUN = "run_20260825T145424Z_full2"
REPLAY_ITEM = "C33"  # multi-hop, and the only category that routes all three legs


# --------------------------------------------------------------------------------
# Task 3: replay
# --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def m3_run():
    path = replay_run.RESULTS_DIR / f"{M3_RUN}.json"

    if not path.exists():
        pytest.skip(f"{path.name} is not present")

    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def events():
    if not replay_run.EVENT_LOG.exists():
        pytest.skip("the JSONL event log is not present")

    return replay_run.load_events()


def test_every_recorded_m3_run_is_identifiable_from_the_event_log(m3_run, events):
    """Replay input, checked offline.

    `find_recorded_run` correlates an eval item with the workflow run id that produced
    it and refuses the correlation unless the event log and the eval report agree on
    the route -- two independent records of the same run. If the OTel migration had
    taken the JSONL log with it, this is the test that would fail.
    """
    correlated = []
    failures = []

    for item in m3_run["results"]:
        try:
            correlated.append(replay_run.find_recorded_run(m3_run, item["id"], events))
        except SystemExit as exc:  # the script's own failure mode
            failures.append((item["id"], str(exc)))

    assert not failures, failures
    assert len(correlated) == len(m3_run["results"])

    # Distinct run ids: one eval item is one workflow run, and a correlation that
    # collapsed two items onto one run would still pass the count above.
    run_ids = {run["run_id"] for run in correlated}
    assert len(run_ids) == len(correlated)


@pytest.mark.requires_gemini
def test_replaying_a_recorded_run_reproduces_its_route(m3_run, events):
    """M7 Task 3: the replayed sql_intent and graph_intent match the recorded ones."""
    load_dotenv(PROJECT_ROOT / ".env")

    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY is not configured")

    recorded = replay_run.find_recorded_run(m3_run, REPLAY_ITEM, events)
    replayed = replay_run.replay_route(recorded["query"])

    for field in replay_run.ASSERTED_FIELDS:
        assert replayed[field] == recorded["route"][field], (
            f"replay diverged on {field}: run {recorded['run_id']} recorded "
            f"{recorded['route'][field]!r}, replay produced {replayed[field]!r}"
        )


# --------------------------------------------------------------------------------
# Task 4: GET /traces/{run_id}
# --------------------------------------------------------------------------------


@pytest.fixture
def traced_run(monkeypatch):
    """The seven-span shape from Task 1, emitted into the memory exporter."""
    monkeypatch.setenv("OTEL_EXPORTER", "memory")
    setup_tracing(force=True)
    memory_store().clear()

    run_id = "test_traces_endpoint"

    with workflow_span(run_id, "which sellers are least reliable") as root:
        with agent_span(run_id, "planner") as span:
            span.set_attribute("sql_intent", "seller_performance")
            record_llm_usage(span, [{"input_tokens": 2900, "output_tokens": 150,
                                     "latency_ms": 1100.0}])

        with retrieval_parent_span(run_id):
            for leg, count in (("sql", 10), ("graph", 10), ("vector", 5)):
                with retrieval_span(run_id, leg) as span:
                    span.set_attribute("record_count", count)

        with agent_span(run_id, "answer") as span:
            record_llm_usage(span, [{"input_tokens": 3450, "output_tokens": 1098,
                                     "latency_ms": 5189.0}])

        root.set_attribute("overall_status", "PASS")

    yield run_id

    memory_store().clear()


@pytest.fixture
def client(project_root):
    load_dotenv(project_root / ".env")

    if not os.getenv("API_BEARER_TOKEN"):
        pytest.skip("API_BEARER_TOKEN is not configured")

    import main as api_main  # src/api is on sys.path via conftest

    with TestClient(api_main.app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['API_BEARER_TOKEN']}"}


def test_the_trace_endpoint_returns_the_spans_for_a_run(client, traced_run):
    """M7 Task 4: at least six spans, each with the four fields the plan names."""
    response = client.get(f"/traces/{traced_run}", headers=auth())

    assert response.status_code == 200, response.text

    body = response.json()

    assert body["run_id"] == traced_run
    assert body["span_count"] >= 6, body["span_count"]
    assert len(body["spans"]) == body["span_count"]

    for span in body["spans"]:
        assert set(span) >= {"name", "status", "attributes", "duration_ms",
                             "parent_span_id"}
        assert span["status"] == "OK", (span["name"], span["status"])
        assert span["duration_ms"] is not None

    names = {span["name"] for span in body["spans"]}
    assert names >= {
        "workflow", "agent.planner", "retrieval",
        "retrieval.sql", "retrieval.graph", "retrieval.vector", "agent.answer",
    }

    # The hierarchy has to survive serialisation, or the response is a flat list of
    # names and "which leg was slow inside retrieval" is unanswerable from it.
    by_id = {span["span_id"]: span for span in body["spans"]}
    roots = [span for span in body["spans"] if span["parent_span_id"] is None]

    assert len(roots) == 1
    assert roots[0]["name"] == "workflow"
    assert body["root_span_id"] == roots[0]["span_id"]

    legs = [s for s in body["spans"] if s["name"].startswith("retrieval.")]
    assert legs
    assert all(by_id[leg["parent_span_id"]]["name"] == "retrieval" for leg in legs)

    # Cost and tokens are attributes on the agent span, not a separate report.
    planner = next(s for s in body["spans"] if s["name"] == "agent.planner")
    assert planner["attributes"]["input_tokens"] == 2900
    assert planner["attributes"]["cost_usd"] > 0


def test_a_parent_span_is_never_listed_below_its_own_child(client, traced_run):
    """Found on a live run, not by reading the code.

    Ordering by start_time alone is not enough. `time_ns()` resolves to roughly 0.4 ms
    on this host, so a parent and the child opened immediately inside it land on the
    same tick; a stable sort then falls back to export order, which is *completion*
    order, and printed `retrieval.vector` above `retrieval` -- its own parent.
    """
    body = client.get(f"/traces/{traced_run}", headers=auth()).json()

    position = {span["span_id"]: index for index, span in enumerate(body["spans"])}

    for span in body["spans"]:
        parent = span["parent_span_id"]

        if parent in position:
            assert position[parent] < position[span["span_id"]], (
                f"{span['name']} is listed above its parent"
            )

    assert body["spans"][0]["name"] == "workflow"


def test_an_unknown_run_is_a_404_not_an_empty_trace(client, traced_run):
    """An empty span list would read as "this run produced no spans"."""
    response = client.get("/traces/no_such_run_id", headers=auth())

    assert response.status_code == 404


def test_the_trace_endpoint_requires_authentication(client, traced_run):
    """A trace carries the query text and the answer's attributes."""
    assert client.get(f"/traces/{traced_run}").status_code == 401
    assert client.get(
        f"/traces/{traced_run}", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
