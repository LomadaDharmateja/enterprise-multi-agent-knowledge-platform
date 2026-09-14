"""Demo mode replays recorded runs and calls nothing (M8 Task 3).

The claim to defend is "zero LLM calls and zero database queries". Asserting it with a
mock would only prove the mock was not called. It is asserted here the way it is
enforced: in demo mode the API never imports the modules that could make a call, so a
subprocess check of `sys.modules` is a statement about what the process *can* do, not
about what this request happened to do.

That check has to run in a subprocess. The rest of this test session imports
`src/api/main.py` in live mode, which loads torch and the database drivers, so an
in-process assertion would be meaningless.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = PROJECT_ROOT / "demo" / "scenarios.json"

# Everything that could reach a model or a database, plus the two modules that import
# them. If none of these is loaded, no call was possible.
FORBIDDEN_MODULES = (
    "torch",
    "transformers",
    "sentence_transformers",
    "sqlalchemy",
    "psycopg2",
    "neo4j",
    "qdrant_client",
    "google.genai",
    "langgraph",
    "agentic_workflow",
    "hybrid_retriever",
)

FLAGSHIP = (
    "For the sellers with the worst late-delivery rates, what do their customers "
    "actually complain about?"
)


@pytest.fixture(scope="module")
def bundle():
    if not BUNDLE.exists():
        pytest.skip("demo/scenarios.json has not been built")

    return json.loads(BUNDLE.read_text(encoding="utf-8"))


@pytest.fixture
def demo_client(monkeypatch):
    """A TestClient with DEMO_MODE on.

    `main` reads DEMO_MODE at import time -- that is the point, it decides which
    modules exist in the process -- so the module is reloaded under the flag.
    """
    import importlib

    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("OTEL_EXPORTER", "memory")

    import demo as demo_module
    import main as api_main

    importlib.reload(demo_module)
    api_main = importlib.reload(api_main)

    with TestClient(api_main.app) as client:
        yield client

    # Leave the module in live mode for whatever runs next.
    monkeypatch.delenv("DEMO_MODE", raising=False)
    importlib.reload(api_main)


# --------------------------------------------------------------------------------
# Zero LLM calls, zero database queries
# --------------------------------------------------------------------------------


SUBPROCESS_PROBE = r"""
import json, os, sys
os.environ["DEMO_MODE"] = "true"
os.environ["OTEL_EXPORTER"] = "memory"

root = sys.argv[1]
for directory in ("api", "observability"):
    sys.path.insert(0, os.path.join(root, "src", directory))

from fastapi.testclient import TestClient
import main

with TestClient(main.app) as client:
    response = client.post("/query", json={"query": sys.argv[2]})
    body = response.json()
    trace = client.get("/traces/" + body["run_id"]).json()

print("RESULT " + json.dumps({
    "status_code": response.status_code,
    "overall_status": body["overall_status"],
    "run_id": body["run_id"],
    "fields": sorted(body),
    "span_count": trace.get("span_count"),
    "loaded": sorted(m for m in sys.argv[3].split(",") if m in sys.modules),
}))
"""


def test_a_demo_request_loads_no_model_and_no_database_driver():
    """The M8 Task 3 exit criterion, measured rather than asserted."""
    completed = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_PROBE, str(PROJECT_ROOT), FLAGSHIP,
         ",".join(FORBIDDEN_MODULES)],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "DEMO_MODE": "true", "PYTHONPATH": ""},
    )

    assert completed.returncode == 0, completed.stderr[-2000:]

    line = next(
        (l for l in completed.stdout.splitlines() if l.startswith("RESULT ")), None
    )

    assert line, completed.stdout[-2000:]

    result = json.loads(line[len("RESULT "):])

    assert result["status_code"] == 200
    assert result["overall_status"] == "PASS"
    assert result["span_count"] >= 3

    assert result["loaded"] == [], (
        f"demo mode loaded modules that can reach a model or a database: "
        f"{result['loaded']}"
    )


# --------------------------------------------------------------------------------
# Structurally indistinguishable from a live response
# --------------------------------------------------------------------------------


def test_the_demo_response_has_exactly_the_live_response_fields(demo_client):
    import main as api_main

    body = demo_client.post("/query", json={"query": FLAGSHIP}).json()

    assert set(body) == set(api_main.QueryResponse.model_fields)


def test_every_recorded_response_satisfies_the_live_response_model(bundle):
    """A field added to QueryResponse must not leave the bundle silently short.

    `from_workflow` fills missing keys with defaults, so a stale bundle would not
    raise -- it would quietly serve empty values.
    """
    import main as api_main

    expected = set(api_main.QueryResponse.model_fields)

    for scenario in bundle["scenarios"]:
        recorded = set(scenario["response"])

        assert recorded == expected, (
            f"{scenario['id']} is missing {expected - recorded} and has "
            f"{recorded - expected} the model does not declare"
        )


def test_a_replayed_answer_carries_its_real_evidence_counts(demo_client, bundle):
    body = demo_client.post("/query", json={"query": FLAGSHIP}).json()
    scenario = next(s for s in bundle["scenarios"] if s["id"] == "C33")

    assert body["run_id"] == scenario["response"]["run_id"]
    assert body["source_summary"]["sql"]["records"] == scenario["evidence"]["sql_records"]
    assert body["source_summary"]["graph"]["records"] == scenario["evidence"]["graph_records"]
    assert body["evaluation_summary"]["grounding_score"] == 5


def test_a_replayed_refusal_is_still_a_refusal(demo_client):
    body = demo_client.post(
        "/query",
        json={"query": "What is the home address of the customer who raised ticket TCK-000001?"},
    ).json()

    assert body["overall_status"] == "REFUSED"
    assert body["answerable"] is False
    assert "personal identifiable information" in body["refusal_reason"]
    assert body["answer_provider"] == "none"


# --------------------------------------------------------------------------------
# An unrecorded question
# --------------------------------------------------------------------------------


def test_an_unrecorded_question_is_not_answered_from_the_closest_recording(demo_client):
    """Returning the nearest recording for an unrelated question would be a lie."""
    body = demo_client.post(
        "/query", json={"query": "what is the airspeed velocity of an unladen swallow"}
    ).json()

    assert body["overall_status"] == "DEMO_NO_SCENARIO"
    assert body["answerable"] is False
    assert "recorded" in body["refusal_reason"].lower()
    assert body["source_summary"] == {}


def test_the_no_scenario_status_is_distinct_from_a_real_refusal(demo_client):
    """"This deployment holds no recording" is a different fact from "the system
    declined this question". Collapsing them would credit the system with a refusal
    it never made."""
    unrecorded = demo_client.post(
        "/query", json={"query": "zzz completely unrelated text zzz"}
    ).json()
    refused = demo_client.post(
        "/query", json={"query": "Which of our sellers are likely to churn next quarter?"}
    ).json()

    assert unrecorded["overall_status"] == "DEMO_NO_SCENARIO"
    assert refused["overall_status"] == "REFUSED"
    assert set(unrecorded) == set(refused)


# --------------------------------------------------------------------------------
# The scenario listing and the trace
# --------------------------------------------------------------------------------


def test_the_scenarios_endpoint_lists_questions_and_routes(demo_client, bundle):
    body = demo_client.get("/demo/scenarios").json()

    assert body["mode"] == "demo"
    assert len(body["scenarios"]) == bundle["scenario_count"]

    for scenario in body["scenarios"]:
        assert scenario["question"]
        assert set(scenario["route"]) >= {
            "sql_intent", "graph_intent", "vector_artifact_groups"
        }
        assert set(scenario["evidence"]) == {
            "sql_records", "graph_records", "vector_records"
        }
        assert scenario["metrics"]["latency_ms"] > 0


def test_the_demo_shows_its_failures_not_only_its_wins(demo_client):
    """A demo of this system that hid the weak cases would misrepresent it.

    Three of the eight recordings are refusals, one scored 2 of 5 on grounding, and
    one is a known false positive. Losing them to a "tidy up the demo" commit is the
    regression this guards.
    """
    scenarios = demo_client.get("/demo/scenarios").json()["scenarios"]

    refusals = [s for s in scenarios if s["answerable"] is False]
    weak = [s for s in scenarios
            if s["grounding_score"] is not None and s["grounding_score"] < 5]
    caveated = [s for s in scenarios if s["caveat"]]

    assert len(refusals) >= 3, [s["id"] for s in scenarios]
    assert weak, "no scenario shows a low grounding score"
    assert len(caveated) >= 3, "the known weaknesses are not labelled"


def test_every_scenario_route_was_checked_against_the_current_system(bundle):
    """The recordings are from the M3 run; M4-M6 changed the routing.

    A demo presenting a stale recording as current behaviour is the exact failure
    this rebuild exists to remove, so each scenario carries a measured verdict.
    """
    unchecked = [
        s["id"] for s in bundle["scenarios"]
        if not s.get("route_reproduces_today", {}).get("checked")
    ]

    assert not unchecked, f"routes never verified: {unchecked}"

    diverged = [
        s["id"] for s in bundle["scenarios"]
        if not s["route_reproduces_today"]["matches"]
    ]

    assert not diverged, (
        f"these recordings no longer match how the system routes today: {diverged}. "
        f"Rebuild the bundle or label them."
    )


def test_the_recorded_trace_is_served_for_a_replayed_run(demo_client, bundle):
    scenario = next(s for s in bundle["scenarios"] if s["id"] == "C33")
    run_id = scenario["response"]["run_id"]

    body = demo_client.get(f"/traces/{run_id}").json()

    assert body["span_count"] == len(scenario["trace"]["spans"])
    assert body["spans"][0]["name"] == "workflow"
    assert body["spans"][0]["parent_span_id"] is None
    assert all(span["duration_ms"] > 0 for span in body["spans"])

    names = [span["name"] for span in body["spans"]]
    assert "agent.planner" in names
    assert "retrieval" in names


def test_a_refusal_trace_has_no_retrieval_span(demo_client, bundle):
    """The refusal path queried nothing, and the trace must not suggest otherwise."""
    scenario = next(s for s in bundle["scenarios"] if s["id"] == "D51")
    body = demo_client.get(f"/traces/{scenario['response']['run_id']}").json()

    names = [span["name"] for span in body["spans"]]

    assert "refusal" in names
    assert "retrieval" not in names
    assert "agent.answer" not in names


def test_an_unknown_run_is_a_404(demo_client):
    assert demo_client.get("/traces/no_such_run").status_code == 404


# --------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------


def test_demo_mode_serves_without_a_bearer_token(demo_client):
    """Deliberate: there is no model call to bill and no database to reach.

    `test_api_security.py` asserts the 401s that still apply in live mode.
    """
    assert demo_client.post("/query", json={"query": FLAGSHIP}).status_code == 200
    assert demo_client.get("/health").status_code == 200
    assert demo_client.get("/demo/scenarios").status_code == 200


def test_demo_health_reports_the_mode_and_probes_nothing(demo_client):
    body = demo_client.get("/health").json()

    assert body["status"] == "ok"
    assert body["mode"] == "demo"
    assert body["dependencies"] == {}
    assert "no database is connected" in body["not_checked"]["postgres"]
    assert body["demo"]["scenario_count"] >= 1
