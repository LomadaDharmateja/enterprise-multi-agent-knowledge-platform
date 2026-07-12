from __future__ import annotations

import csv
import json
import os
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_OBSERVABILITY_DIR = Path("reports/observability")
EVENT_LOG_FILE = "workflow_events.jsonl"
METRICS_FILE = "workflow_metrics.csv"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_observability_dir() -> Path:
    return Path(os.getenv("OBSERVABILITY_DIR", str(DEFAULT_OBSERVABILITY_DIR)))


def get_event_log_path() -> Path:
    return get_observability_dir() / EVENT_LOG_FILE


def get_metrics_path() -> Path:
    return get_observability_dir() / METRICS_FILE


def ensure_observability_dir() -> None:
    get_observability_dir().mkdir(parents=True, exist_ok=True)


def new_run_id(prefix: str = "workflow") -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{prefix}_{timestamp}_{short_uuid}"


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def record_event(
    run_id: str,
    component: str,
    event_type: str,
    operation: str | None = None,
    status: str | None = None,
    metadata: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    ensure_observability_dir()

    record = {
        "timestamp": utc_now_iso(),
        "run_id": run_id,
        "component": component,
        "event_type": event_type,
        "operation": operation,
        "status": status,
        "metadata": metadata or {},
        "output": output or {},
        "error": error,
    }

    append_jsonl(get_event_log_path(), record)


def record_metric(
    run_id: str,
    component: str,
    operation: str,
    metric_name: str,
    metric_value: float,
    unit: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    ensure_observability_dir()

    path = get_metrics_path()
    file_exists = path.exists()

    fieldnames = [
        "timestamp",
        "run_id",
        "component",
        "operation",
        "metric_name",
        "metric_value",
        "unit",
        "metadata_json",
    ]

    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerow(
            {
                "timestamp": utc_now_iso(),
                "run_id": run_id,
                "component": component,
                "operation": operation,
                "metric_name": metric_name,
                "metric_value": metric_value,
                "unit": unit,
                "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, default=str),
            }
        )


@contextmanager
def trace_span(
    run_id: str,
    component: str,
    operation: str,
    metadata: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    span_id = f"span_{uuid.uuid4().hex[:12]}"
    start_time = time.perf_counter()

    span_data: dict[str, Any] = {
        "span_id": span_id,
        "run_id": run_id,
        "component": component,
        "operation": operation,
        "status": "PASS",
        "metadata": metadata or {},
        "output": {},
    }

    record_event(
        run_id=run_id,
        component=component,
        event_type="span_start",
        operation=operation,
        status="RUNNING",
        metadata={
            "span_id": span_id,
            **(metadata or {}),
        },
    )

    try:
        yield span_data

    except Exception as exc:
        span_data["status"] = "FAIL"

        error_payload = {
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
        }

        span_data["error"] = error_payload

        record_event(
            run_id=run_id,
            component=component,
            event_type="span_error",
            operation=operation,
            status="FAIL",
            metadata={
                "span_id": span_id,
                **(metadata or {}),
            },
            error=error_payload,
        )

        raise

    finally:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        record_event(
            run_id=run_id,
            component=component,
            event_type="span_end",
            operation=operation,
            status=span_data.get("status", "PASS"),
            metadata={
                "span_id": span_id,
                **(metadata or {}),
            },
            output=span_data.get("output", {}),
        )

        record_metric(
            run_id=run_id,
            component=component,
            operation=operation,
            metric_name="duration_ms",
            metric_value=duration_ms,
            unit="ms",
            metadata={
                "span_id": span_id,
                "status": span_data.get("status", "PASS"),
            },
        )