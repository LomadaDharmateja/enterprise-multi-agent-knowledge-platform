"""OpenTelemetry tracing for the agentic workflow (M7 Task 1).

Replaces the bespoke JSONL span log as the *trace*. The JSONL run file stays: it is the
replay input and M7 Task 3 depends on it.

Why the bespoke version had to go, beyond "standards are nice":

    `trace_span` initialised `span_data["status"] = "PASS"` and only downgraded it if an
    exception propagated through that specific context manager. A crash BETWEEN spans --
    in the LangGraph edge, in a node before its span opened, in the harness -- left every
    recorded span marked PASS and no record of the failure at all. The trace said the run
    succeeded.

OTel inverts that. A span's status is UNSET until something sets it; `record_exception`
plus `set_status(ERROR)` are what a failure looks like, and a span whose `end()` never
runs is visibly unended rather than quietly successful.

Exporters:
  console (default) -- human-readable, no infrastructure
  otlp              -- OTEL_EXPORTER=otlp, honours OTEL_EXPORTER_OTLP_ENDPOINT
  memory            -- OTEL_EXPORTER=memory, for tests and the /traces endpoint
  none              -- tracing off
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status, StatusCode

SERVICE_NAME = "enterprise-agentic-workflow"

# Cost is an attribute, not a derived report. gemini-3.1-flash-lite published rates.
INPUT_USD_PER_TOKEN = 0.25 / 1e6
OUTPUT_USD_PER_TOKEN = 1.50 / 1e6


class InMemorySpanStore(SpanExporter):
    """Keeps finished spans in process, indexed by run id.

    Backs `GET /traces/{run_id}` (M7 Task 4) so a trace is readable without standing up
    a collector. Bounded, because an unbounded trace buffer in a long-running process is
    a memory leak wearing a lanyard.
    """

    def __init__(self, max_runs: int = 50) -> None:
        self.max_runs = max_runs
        self._runs: dict[str, list[ReadableSpan]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def export(self, spans) -> SpanExportResult:
        with self._lock:
            for span in spans:
                run_id = str((span.attributes or {}).get("run_id", "unknown"))

                if run_id not in self._runs:
                    self._runs[run_id] = []
                    self._order.append(run_id)

                    while len(self._order) > self.max_runs:
                        self._runs.pop(self._order.pop(0), None)

                self._runs[run_id].append(span)

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:  # pragma: no cover - SDK contract
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # pragma: no cover
        return True

    # ---------------------------------------------------------------- reading

    def run_ids(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def spans_for(self, run_id: str) -> list[ReadableSpan]:
        with self._lock:
            return list(self._runs.get(run_id, []))

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            self._order.clear()


_MEMORY_STORE = InMemorySpanStore()
_PROVIDER: TracerProvider | None = None
_SETUP_LOCK = threading.Lock()


def memory_store() -> InMemorySpanStore:
    return _MEMORY_STORE


def _build_exporters() -> list[tuple[SpanExporter, bool]]:
    """(exporter, use_simple_processor). Simple = synchronous, which tests need."""
    choice = os.getenv("OTEL_EXPORTER", "console").strip().lower()

    if choice == "none":
        return [(_MEMORY_STORE, True)]

    if choice == "memory":
        return [(_MEMORY_STORE, True)]

    if choice == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        endpoint = os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
        ).rstrip("/")

        return [
            (OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"), False),
            (_MEMORY_STORE, True),
        ]

    # console default: always alongside the memory store, so /traces works locally.
    return [(ConsoleSpanExporter(), False), (_MEMORY_STORE, True)]


def setup_tracing(force: bool = False) -> TracerProvider:
    global _PROVIDER

    with _SETUP_LOCK:
        if _PROVIDER is not None and not force:
            return _PROVIDER

        provider = TracerProvider(
            resource=Resource.create({"service.name": SERVICE_NAME})
        )

        for exporter, simple in _build_exporters():
            provider.add_span_processor(
                SimpleSpanProcessor(exporter) if simple else BatchSpanProcessor(exporter)
            )

        trace.set_tracer_provider(provider)
        _PROVIDER = provider

        return provider


def get_tracer() -> trace.Tracer:
    setup_tracing()
    return trace.get_tracer(SERVICE_NAME)


def _flatten(value: Any) -> Any:
    """OTel attributes must be primitives or homogeneous sequences of them."""
    if value is None:
        return None

    if isinstance(value, (str, bool, int, float)):
        return value

    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]

    return str(value)


def set_attributes(span, attributes: dict[str, Any]) -> None:
    for key, value in attributes.items():
        flat = _flatten(value)

        if flat is not None:
            span.set_attribute(key, flat)


@contextmanager
def workflow_span(run_id: str, query: str) -> Iterator[Any]:
    """Root span. Everything else is a child of this."""
    with get_tracer().start_as_current_span("workflow") as span:
        set_attributes(span, {"run_id": run_id, "query": query})

        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise
        else:
            if span.status.status_code is StatusCode.UNSET:
                span.set_status(Status(StatusCode.OK))


@contextmanager
def agent_span(run_id: str, agent_name: str, **attributes: Any) -> Iterator[Any]:
    """One LLM agent call. Cost and tokens go on as attributes, not into a report."""
    with get_tracer().start_as_current_span(f"agent.{agent_name}") as span:
        set_attributes(span, {"run_id": run_id, "agent_name": agent_name, **attributes})

        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise
        else:
            if span.status.status_code is StatusCode.UNSET:
                span.set_status(Status(StatusCode.OK))


@contextmanager
def retrieval_parent_span(run_id: str, **attributes: Any) -> Iterator[Any]:
    """Parent of the three leg spans, so the trace shows retrieval as one unit."""
    with get_tracer().start_as_current_span("retrieval") as span:
        set_attributes(span, {"run_id": run_id, **attributes})

        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise
        else:
            if span.status.status_code is StatusCode.UNSET:
                span.set_status(Status(StatusCode.OK))


@contextmanager
def retrieval_span(run_id: str, leg: str, **attributes: Any) -> Iterator[Any]:
    """One retrieval leg: sql, graph or vector."""
    with get_tracer().start_as_current_span(f"retrieval.{leg}") as span:
        set_attributes(span, {"run_id": run_id, "retrieval_leg": leg, **attributes})

        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise
        else:
            if span.status.status_code is StatusCode.UNSET:
                span.set_status(Status(StatusCode.OK))


def record_llm_usage(span, calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Put tokens, cost and latency on the span. Returns the totals for the caller."""
    input_tokens = sum(c.get("input_tokens") or 0 for c in calls)
    output_tokens = sum(c.get("output_tokens") or 0 for c in calls)
    latency_ms = sum(c.get("latency_ms") or 0.0 for c in calls)
    cost = input_tokens * INPUT_USD_PER_TOKEN + output_tokens * OUTPUT_USD_PER_TOKEN

    totals = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(cost, 8),
        "latency_ms": round(latency_ms, 1),
        "llm_calls": len(calls),
        # None means the provider did not report usage. Distinct from zero.
        "usage_missing_calls": sum(1 for c in calls if c.get("input_tokens") is None),
    }

    set_attributes(span, totals)

    return totals


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    """Human-readable form for GET /traces/{run_id}."""
    context = span.get_span_context()
    parent = span.parent

    duration_ms = None

    if span.start_time is not None and span.end_time is not None:
        duration_ms = round((span.end_time - span.start_time) / 1e6, 3)

    return {
        "name": span.name,
        "span_id": format(context.span_id, "016x"),
        "trace_id": format(context.trace_id, "032x"),
        "parent_span_id": format(parent.span_id, "016x") if parent else None,
        "status": span.status.status_code.name,
        "status_description": span.status.description,
        "duration_ms": duration_ms,
        "ended": span.end_time is not None,
        "attributes": dict(span.attributes or {}),
        "events": [
            {
                "name": event.name,
                "attributes": dict(event.attributes or {}),
            }
            for event in (span.events or [])
        ],
    }
