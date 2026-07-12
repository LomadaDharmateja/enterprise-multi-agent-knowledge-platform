from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_EVENT_LOG_PATH = Path("reports/observability/workflow_events.jsonl")
DEFAULT_METRICS_PATH = Path("reports/observability/workflow_metrics.csv")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records = []

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            records.append(json.loads(line))

    return records


def summarize_runs(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for event in events:
        grouped[event["run_id"]].append(event)

    summaries = []

    for run_id, run_events in grouped.items():
        sorted_events = sorted(run_events, key=lambda item: item["timestamp"])

        span_end_events = [
            event for event in sorted_events if event.get("event_type") == "span_end"
        ]

        error_events = [
            event for event in sorted_events if event.get("event_type") == "span_error"
        ]

        components = []

        for event in span_end_events:
            components.append(
                {
                    "component": event.get("component"),
                    "operation": event.get("operation"),
                    "status": event.get("status"),
                    "output": event.get("output", {}),
                }
            )

        status = "FAIL" if error_events else "PASS"

        summaries.append(
            {
                "run_id": run_id,
                "started_at": sorted_events[0]["timestamp"] if sorted_events else None,
                "finished_at": sorted_events[-1]["timestamp"] if sorted_events else None,
                "status": status,
                "event_count": len(sorted_events),
                "error_count": len(error_events),
                "components": components,
            }
        )

    summaries = sorted(
        summaries,
        key=lambda item: item["finished_at"] or "",
        reverse=True,
    )

    return summaries[:limit]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize local workflow observability events."
    )

    parser.add_argument(
        "--events",
        type=Path,
        default=DEFAULT_EVENT_LOG_PATH,
        help="Path to workflow_events.jsonl.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of latest runs to print.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    events = load_jsonl(args.events)

    if not events:
        print("No observability events found yet.")
        print(f"Expected event log path: {args.events}")
        return

    summaries = summarize_runs(events, limit=args.limit)

    print("\nWorkflow Observability Report")
    print("-----------------------------")
    print(f"Event log: {args.events}")
    print(f"Total events: {len(events)}")
    print(f"Latest runs shown: {len(summaries)}")

    for summary in summaries:
        print("\nRun")
        print("---")
        print(f"Run ID: {summary['run_id']}")
        print(f"Status: {summary['status']}")
        print(f"Started: {summary['started_at']}")
        print(f"Finished: {summary['finished_at']}")
        print(f"Events: {summary['event_count']}")
        print(f"Errors: {summary['error_count']}")

        print("Components:")

        for component in summary["components"]:
            print(
                f"  - {component['component']} / {component['operation']} "
                f"({component['status']})"
            )


if __name__ == "__main__":
    main()