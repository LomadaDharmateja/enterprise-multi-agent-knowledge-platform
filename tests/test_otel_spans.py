"""OpenTelemetry spans for the workflow (M7 Tasks 1 and 2).

The bespoke tracer this replaces initialised every span to `status = "PASS"` and only
downgraded it if an exception propagated through that specific context manager. A crash
BETWEEN spans left a trace in which every recorded event said PASS and the failure
appeared nowhere. These tests assert that the replacement cannot do that.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("OTEL_EXPORTER", "memory")

from otel import (  # noqa: E402
    agent_span,
    retrieval_parent_span,
    memory_store,
    record_llm_usage,
    retrieval_span,
    setup_tracing,
    span_to_dict,
    workflow_span,
)


@pytest.fixture(autouse=True)
def clean_spans(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER", "memory")
    setup_tracing(force=True)
    memory_store().clear()
    yield
    memory_store().clear()


def spans_for(run_id):
    return [span_to_dict(s) for s in memory_store().spans_for(run_id)]


def by_name(spans, name):
    return next(s for s in spans if s["name"] == name)


# --------------------------------------------------------------------------------
# Task 1: a completed workflow produces a root span with child spans
# --------------------------------------------------------------------------------


def test_a_completed_workflow_produces_a_root_span_with_at_least_three_children():
    run_id = "test_run_complete"

    with workflow_span(run_id, "which sellers are least reliable") as root:
        with agent_span(run_id, "planner") as span:
            record_llm_usage(span, [{"input_tokens": 2900, "output_tokens": 150,
                                     "latency_ms": 1100.0}])

        with retrieval_parent_span(run_id):
            for leg in ("sql", "graph", "vector"):
                with retrieval_span(run_id, leg) as span:
                    span.set_attribute("record_count", 10)

        with agent_span(run_id, "answer") as span:
            record_llm_usage(span, [{"input_tokens": 2300, "output_tokens": 680,
                                     "latency_ms": 3500.0}])

        with agent_span(run_id, "evaluator") as span:
            record_llm_usage(span, [{"input_tokens": 6500, "output_tokens": 240,
                                     "latency_ms": 2200.0}])

        root.set_attribute("overall_status", "PASS")

    spans = spans_for(run_id)

    root_spans = [s for s in spans if s["parent_span_id"] is None]
    assert len(root_spans) == 1, f"expected exactly one root span, got {len(root_spans)}"

    root = root_spans[0]
    assert root["name"] == "workflow"

    children = [s for s in spans if s["parent_span_id"] == root["span_id"]]
    assert len(children) >= 3, f"expected >= 3 child spans, got {len(children)}"

    assert {s["name"] for s in children} >= {
        "agent.planner", "agent.answer", "agent.evaluator", "retrieval",
    }

    # The three legs hang off the retrieval parent, not off the workflow root.
    parent = by_name(spans, "retrieval")
    legs = [s for s in spans if s["parent_span_id"] == parent["span_id"]]

    assert {s["name"] for s in legs} == {
        "retrieval.sql", "retrieval.graph", "retrieval.vector",
    }

    assert all(s["status"] == "OK" for s in spans), [
        (s["name"], s["status"]) for s in spans
    ]
    assert all(s["ended"] for s in spans)


def test_agent_spans_carry_tokens_cost_and_latency():
    """The rebuild plan asks for cost and tokens as span attributes, not a report."""
    run_id = "test_run_attributes"

    with workflow_span(run_id, "q"):
        with agent_span(run_id, "answer") as span:
            totals = record_llm_usage(
                span, [{"input_tokens": 1000, "output_tokens": 100, "latency_ms": 250.0}]
            )

    attributes = by_name(spans_for(run_id), "agent.answer")["attributes"]

    assert attributes["input_tokens"] == 1000
    assert attributes["output_tokens"] == 100
    assert attributes["latency_ms"] == 250.0
    assert attributes["agent_name"] == "answer"
    assert attributes["run_id"] == run_id

    # 1000 * 0.25/1M + 100 * 1.50/1M
    assert attributes["cost_usd"] == pytest.approx(0.00040, abs=1e-8)
    assert totals["cost_usd"] == attributes["cost_usd"]


def test_missing_usage_is_counted_not_treated_as_zero():
    run_id = "test_run_missing_usage"

    with workflow_span(run_id, "q"):
        with agent_span(run_id, "answer") as span:
            record_llm_usage(
                span,
                [
                    {"input_tokens": None, "output_tokens": None, "latency_ms": 100.0},
                    {"input_tokens": 500, "output_tokens": 50, "latency_ms": 100.0},
                ],
            )

    attributes = by_name(spans_for(run_id), "agent.answer")["attributes"]

    assert attributes["usage_missing_calls"] == 1
    assert attributes["llm_calls"] == 2


def test_retrieval_spans_record_evidence_linking():
    run_id = "test_run_retrieval"

    with workflow_span(run_id, "q"):
        with retrieval_parent_span(run_id):
            with retrieval_span(run_id, "sql") as span:
                time.sleep(0.01)  # real work, so the duration is a measurement
                span.set_attribute("record_count", 2)
                span.set_attribute("evidence_linked_filtering_applied", True)
                span.set_attribute("evidence_link_fallback", True)

    attributes = by_name(spans_for(run_id), "retrieval.sql")["attributes"]

    assert attributes["retrieval_leg"] == "sql"
    assert attributes["record_count"] == 2
    assert attributes["evidence_linked_filtering_applied"] is True
    assert attributes["evidence_link_fallback"] is True

    leg = by_name(spans_for(run_id), "retrieval.sql")
    assert leg["duration_ms"] > 0, (
        "a retrieval leg reported zero duration -- the span is being reconstructed "
        "after the fact rather than bracketing the actual database work"
    )


# --------------------------------------------------------------------------------
# Task 2: a crash cannot leave a trace that says everything passed
# --------------------------------------------------------------------------------


def test_a_crashed_workflow_produces_an_error_span_not_an_ok_one():
    run_id = "test_run_crash"

    with pytest.raises(RuntimeError):
        with workflow_span(run_id, "a question that will fail"):
            with agent_span(run_id, "planner") as span:
                span.set_attribute("sql_intent", "seller_performance")

            with agent_span(run_id, "answer"):
                raise RuntimeError("Gemini returned an empty response")

    spans = spans_for(run_id)
    statuses = {s["name"]: s["status"] for s in spans}

    assert statuses["agent.answer"] == "ERROR", statuses
    assert statuses["workflow"] == "ERROR", (
        "the crash did not propagate to the root span -- the trace would report a "
        "successful run"
    )

    assert statuses["agent.planner"] == "OK", (
        "the span that genuinely succeeded should not be retroactively failed"
    )

    assert all(s["ended"] for s in spans), "a span was left unended"


def test_the_error_span_carries_the_exception():
    """A failing run must be diagnosable from the trace alone -- the M7 exit criterion."""
    run_id = "test_run_exception_detail"

    with pytest.raises(ValueError):
        with workflow_span(run_id, "q"):
            with agent_span(run_id, "planner"):
                raise ValueError("Invalid SQL intent from Gemini: None")

    span = by_name(spans_for(run_id), "agent.planner")

    assert span["status"] == "ERROR"
    assert "ValueError" in (span["status_description"] or "")
    assert "Invalid SQL intent" in (span["status_description"] or "")

    exception_events = [e for e in span["events"] if e["name"] == "exception"]
    assert exception_events, "no exception event recorded on the span"

    attributes = exception_events[0]["attributes"]
    assert attributes.get("exception.type", "").endswith("ValueError")
    assert "Invalid SQL intent" in attributes.get("exception.message", "")


def test_a_crash_between_spans_still_fails_the_root_span():
    """The exact defect the rebuild plan names.

    The old tracer set status="PASS" at span start and only downgraded on an exception
    inside that span's own context manager. A crash in the code BETWEEN two spans left
    every recorded span saying PASS. Here the failure happens with no agent span open.
    """
    run_id = "test_run_between_spans"

    with pytest.raises(KeyError):
        with workflow_span(run_id, "q"):
            with agent_span(run_id, "planner") as span:
                span.set_attribute("sql_intent", "seller_performance")

            # No span is open here. This is the gap the old tracer could not see.
            raise KeyError("planned_route")

    spans = spans_for(run_id)
    statuses = {s["name"]: s["status"] for s in spans}

    assert statuses["agent.planner"] == "OK"
    assert statuses["workflow"] == "ERROR", (
        "a crash between spans left the trace reporting success -- this is the "
        "defect the migration exists to fix"
    )

    root = by_name(spans, "workflow")
    assert "KeyError" in (root["status_description"] or "")


def test_no_span_defaults_to_a_passing_status():
    """A span must not be born successful.

    The bespoke tracer initialised status="PASS". OTel starts UNSET and only reaches OK
    because the context manager sets it on clean exit -- so an abandoned span is never
    silently a success.
    """
    from opentelemetry.trace import StatusCode

    from otel import get_tracer

    span = get_tracer().start_span("never_finished")

    try:
        assert span.status.status_code is StatusCode.UNSET, (
            "a freshly created span already carries a non-UNSET status"
        )
    finally:
        span.end()


# --------------------------------------------------------------------------------
# The JSONL replay log must survive the migration
# --------------------------------------------------------------------------------


def test_the_jsonl_event_log_is_still_written(tmp_path, monkeypatch):
    """M7 Task 3 replays from the JSONL run file; OTel must not have removed it."""
    monkeypatch.setenv("OBSERVABILITY_DIR", str(tmp_path))

    import observability

    observability.record_event(
        run_id="test_run_jsonl",
        component="test",
        event_type="span_end",
        operation="probe",
        status="PASS",
    )

    log = Path(tmp_path) / "workflow_events.jsonl"

    assert log.exists(), "the JSONL replay log was not written"
    assert "test_run_jsonl" in log.read_text(encoding="utf-8")
