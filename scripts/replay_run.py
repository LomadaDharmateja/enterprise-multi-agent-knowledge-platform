"""Deterministic replay of a recorded run (M7 Task 3).

A recorded run is replayable because the JSONL event log survived the OTel migration:
it carries the run id, the exact query text, and the route the planner produced. This
script finds a recorded run, re-plans the same query against the current system, and
compares the two routes field by field.

What this tests is not "the LLM is deterministic" -- it is not, and no temperature is
pinned -- but that the *recorded* route and the *replayed* route agree, which is what
makes a stored run usable as a regression fixture and as M8's demo mode. Divergence is
reported, never smoothed over.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

for _d in (
    PROJECT_ROOT / "src" / "planning",
    PROJECT_ROOT / "src" / "orchestration",
    PROJECT_ROOT / "src" / "retrieval",
    PROJECT_ROOT / "src" / "observability",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

EVENT_LOG = PROJECT_ROOT / "reports" / "observability" / "workflow_events.jsonl"
RESULTS_DIR = PROJECT_ROOT / "tests" / "eval" / "results"

# The two fields M7 Task 3 asserts on. The rest are reported but not asserted: filters
# and sort keys are resolved against live vocabulary and are a separate contract.
ASSERTED_FIELDS = ("sql_intent", "graph_intent")
REPORTED_FIELDS = ASSERTED_FIELDS + ("vector_artifact_groups", "sql_sort_by")


def load_events(path: Path = EVENT_LOG) -> list[dict[str, Any]]:
    events = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn last line is not a reason to fail the replay

    return events


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp)


def recorded_runs(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """run_id -> {query, route, timestamp}, from the planner span_end events."""
    runs: dict[str, dict[str, Any]] = {}

    for event in events:
        if event.get("component") != "gemini_planner_agent":
            continue

        if event.get("event_type") != "span_end":
            continue

        output = event.get("output") or {}
        query = (event.get("metadata") or {}).get("query")

        if not query or "sql_intent" not in output:
            continue

        runs[event["run_id"]] = {
            "run_id": event["run_id"],
            "query": query,
            "timestamp": event["timestamp"],
            "status": event.get("status"),
            "route": {field: output.get(field) for field in REPORTED_FIELDS},
        }

    return runs


def find_recorded_run(
    eval_run: dict[str, Any],
    item_id: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Correlate an eval item with the workflow run id that produced it.

    Correlation is by exact query text inside the eval run's wall-clock window. The eval
    runner is sequential and one item is one workflow run, so the window plus the query
    identifies it; the same question asked in a later run falls outside the window.
    """
    item = next((i for i in eval_run["results"] if i["id"] == item_id), None)

    if item is None:
        raise SystemExit(f"item {item_id} is not in {eval_run['run_id']}")

    finished = _parse(eval_run["generated_at"]).timestamp()
    window_start = finished - ((eval_run.get("wall_clock_ms") or 0) / 1000.0) - 60

    candidates = [
        run
        for run in recorded_runs(events).values()
        if run["query"] == item["question"]
        and window_start <= _parse(run["timestamp"]).timestamp() <= finished + 60
    ]

    if not candidates:
        raise SystemExit(
            f"no recorded workflow run in {EVENT_LOG.name} matches item {item_id} "
            f"within the window of eval run {eval_run['run_id']}"
        )

    candidates.sort(key=lambda run: run["timestamp"])
    recorded = candidates[-1]

    # The event log and the eval report are two independent records of the same run. If
    # they disagree the correlation is wrong and the replay would be measuring nothing.
    for field in ASSERTED_FIELDS:
        if recorded["route"][field] != item["actual"][field]:
            raise SystemExit(
                f"correlation mismatch on {field}: event log says "
                f"{recorded['route'][field]!r}, eval report says {item['actual'][field]!r}"
            )

    recorded["eval_item_id"] = item_id
    recorded["eval_run_id"] = eval_run["run_id"]

    return recorded


def replay_route(query: str) -> dict[str, Any]:
    from gemini_query_planner import plan_query_with_gemini

    plan = plan_query_with_gemini(query)

    return {
        "sql_intent": plan.get("sql_intent"),
        "graph_intent": plan.get("graph_intent"),
        "vector_artifact_groups": plan.get("vector_artifact_groups"),
        "sql_sort_by": (plan.get("sql_plan") or {}).get("sort_by"),
    }


def compare(recorded: dict[str, Any], replayed: dict[str, Any]) -> dict[str, Any]:
    fields = {
        field: {
            "recorded": recorded[field],
            "replayed": replayed.get(field),
            "match": recorded[field] == replayed.get(field),
            "asserted": field in ASSERTED_FIELDS,
        }
        for field in REPORTED_FIELDS
    }

    return {
        "fields": fields,
        "asserted_fields_match": all(v["match"] for v in fields.values() if v["asserted"]),
        "all_fields_match": all(v["match"] for v in fields.values()),
    }


def replay_eval_item(
    eval_run_stem: str,
    item_id: str,
    repeat: int = 1,
) -> dict[str, Any]:
    """The whole operation, importable so the test and the CLI run the same code."""
    eval_run = json.loads(
        (RESULTS_DIR / f"{eval_run_stem}.json").read_text(encoding="utf-8")
    )
    recorded = find_recorded_run(eval_run, item_id, load_events())

    attempts = []

    for attempt in range(1, repeat + 1):
        replayed = replay_route(recorded["query"])
        attempts.append({"attempt": attempt, "replayed": replayed,
                         **compare(recorded["route"], replayed)})

    return {
        "recorded": recorded,
        "attempts": attempts,
        "asserted_fields": list(ASSERTED_FIELDS),
        "asserted_match_count": sum(1 for a in attempts if a["asserted_fields_match"]),
        "attempt_count": len(attempts),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eval-run", default="run_20260825T145424Z_full2",
                    help="M3 results file stem under tests/eval/results/")
    ap.add_argument("--item", default="A10", help="eval item id to replay")
    ap.add_argument("--repeat", type=int, default=1,
                    help="replay N times; >1 measures route stability, it does not "
                         "change what is asserted")
    ap.add_argument("--report", type=Path,
                    default=PROJECT_ROOT / "reports" / "m7_replay.json")
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")

    report = replay_eval_item(args.eval_run, args.item, args.repeat)
    recorded = report["recorded"]

    print(f"eval run    : {recorded['eval_run_id']}  item {recorded['eval_item_id']}")
    print(f"run_id      : {recorded['run_id']}")
    print(f"recorded at : {recorded['timestamp']}")
    print(f"query       : {recorded['query']}")
    print(f"recorded    : {recorded['route']}")

    for attempt in report["attempts"]:
        print(f"\nreplay {attempt['attempt']}    : {attempt['replayed']}")

        for field, detail in attempt["fields"].items():
            mark = "OK  " if detail["match"] else "DIFF"
            tag = "asserted" if detail["asserted"] else "reported"
            print(f"  [{mark}] {field:<24} {tag}  "
                  f"{detail['recorded']!r} -> {detail['replayed']!r}")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"\nasserted fields matched on {report['asserted_match_count']}/"
          f"{report['attempt_count']} replays")
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
