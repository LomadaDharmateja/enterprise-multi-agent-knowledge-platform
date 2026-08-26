"""M3 evaluation runner.

Runs items from `tests/eval/eval_set_v1.json` through the REAL workflow -- the same
LangGraph pipeline the API serves, with live PostgreSQL, Neo4j, Qdrant and Gemini. No
mocks, no recorded fixtures: a runner that stubs the thing it measures is how the
project got twelve validators reporting PASS against an inert retrieval layer.

Writes one record per item to tests/eval/results/run_{timestamp}.json.

Skipping: items whose flags intersect --skip-flags are not run and are logged in a
separate `skipped` block with the reason. The default skip is `requires_fixture` (E83),
which cannot run until its poisoned Qdrant document is upserted -- a live mutation that
needs explicit approval. Note that `manual_review_at_calibration` (C43) is deliberately
NOT skipped: that flag means a human must review the judgement later, not that the item
should go unmeasured.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

for _d in (
    PROJECT_ROOT / "src" / "orchestration",
    PROJECT_ROOT / "src" / "observability",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from agentic_workflow import run_agentic_workflow  # noqa: E402
from llm_usage import collect  # noqa: E402

EVAL_SET = PROJECT_ROOT / "tests" / "eval" / "eval_set_v1.json"
RESULTS_DIR = PROJECT_ROOT / "tests" / "eval" / "results"

DEFAULT_SKIP_FLAGS = ("requires_fixture",)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarise_calls(calls: list[dict]) -> dict:
    """Per-agent and total token accounting.

    None means the provider did not report a count. It is propagated rather than
    coerced to zero, so a missing measurement never reads as a free call.
    """
    by_agent: dict[str, dict] = {}

    for call in calls:
        entry = by_agent.setdefault(
            call["agent"],
            {"calls": 0, "input_tokens": 0, "output_tokens": 0,
             "latency_ms": 0.0, "missing_usage": 0},
        )
        entry["calls"] += 1
        entry["latency_ms"] = round(entry["latency_ms"] + call["latency_ms"], 1)

        if call["input_tokens"] is None or call["output_tokens"] is None:
            entry["missing_usage"] += 1
        else:
            entry["input_tokens"] += call["input_tokens"]
            entry["output_tokens"] += call["output_tokens"]

    totals = {
        "calls": sum(a["calls"] for a in by_agent.values()),
        "input_tokens": sum(a["input_tokens"] for a in by_agent.values()),
        "output_tokens": sum(a["output_tokens"] for a in by_agent.values()),
        "missing_usage": sum(a["missing_usage"] for a in by_agent.values()),
    }
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]

    return {"by_agent": by_agent, "totals": totals, "calls": calls}


RETRY_HINT = re.compile(r"retry in ([0-9.]+)s", re.I)


def retry_delay_from(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if the error is not retryable.

    The Gemini 429 carries its own hint ("Please retry in 38.1s"); honouring it is
    both faster and more polite than a guessed backoff.
    """
    message = str(exc)

    if "429" not in message and "RESOURCE_EXHAUSTED" not in message:
        return None

    match = RETRY_HINT.search(message)

    if match:
        return float(match.group(1)) + 2.0

    return min(60.0, 5.0 * (2 ** attempt))


def run_item(item: dict, output_root: Path, max_retries: int = 4) -> dict:
    qid = item["id"]
    question = item["question"]

    record: dict = {
        "id": qid,
        "category": item["category"],
        "question": question,
        "flags": item["flags"],
        "expected": {
            "answerable": item["answerable"],
            "route": item["expected_route"],
            "refusal": item["expected_refusal"],
        },
    }

    started = time.perf_counter()
    calls: list = []
    final = None
    rate_limit_retries = 0

    for attempt in range(max_retries + 1):
        try:
            with collect() as calls:
                final = run_agentic_workflow(
                    query=question,
                    output_dir=output_root / qid,
                )
            break
        except Exception as exc:  # noqa: BLE001 -- a crash is a measurement
            delay = retry_delay_from(exc, attempt)

            if delay is not None and attempt < max_retries:
                rate_limit_retries += 1
                print(f"    rate limited; sleeping {delay:.0f}s "
                      f"(retry {rate_limit_retries}/{max_retries})")
                time.sleep(delay)
                continue

            record["outcome"] = "crashed"
            record["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            record["rate_limit_retries"] = rate_limit_retries
            record["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback_tail": traceback.format_exc().strip().splitlines()[-1],
            }
            record["llm_usage"] = summarise_calls(list(calls))
            return record

    latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    record["rate_limit_retries"] = rate_limit_retries

    route = final.get("planned_route") or {}
    sources = final.get("source_summary") or {}
    evaluation = final.get("evaluation_summary") or {}
    answerable = final.get("answerable", True)

    answer_text = ""
    answer_path = (final.get("output_files") or {}).get("answer_json")

    if answer_path and Path(answer_path).exists():
        answer_text = read_json(Path(answer_path)).get("answer_text", "")

    raw_path = (final.get("output_files") or {}).get("raw_retrieval")
    sql_records: list = []
    graph_records: list = []
    vector_records: list = []

    if raw_path and Path(raw_path).exists():
        raw = read_json(Path(raw_path))
        results = raw["retrieval_results"]
        sql_records = results["sql"]["records"]
        graph_records = results["graph"]["records"]
        for group in results["vector"]["results_by_artifact_group"].values():
            vector_records.extend(group)

    record.update({
        "outcome": "refused" if answerable is False else "completed",
        "latency_ms": latency_ms,
        "actual": {
            "answerable": answerable,
            "refusal_reason": final.get("refusal_reason"),
            "sql_intent": route.get("sql_intent"),
            "graph_intent": route.get("graph_intent"),
            "vector_artifact_groups": route.get("vector_artifact_groups") or [],
            "sql_filters": (route.get("sql_plan") or {}).get("filters"),
            "sql_sort_by": (route.get("sql_plan") or {}).get("sort_by"),
            "graph_filters": (route.get("graph_plan") or {}).get("filters"),
            "dropped_filters": route.get("dropped_filters") or [],
        },
        "evidence": {
            "sql_records": (sources.get("sql") or {}).get("records", 0),
            "graph_records": (sources.get("graph") or {}).get("records", 0),
            "vector_records": (sources.get("vector") or {}).get("records", 0),
            "sql_record_sample": sql_records[:10],
            "graph_record_sample": graph_records[:10],
            "vector_record_sample": vector_records[:10],
        },
        "evidence_quality": final.get("evidence_quality"),
        "answer_text": answer_text,
        "answer_length_chars": len(answer_text),
        "evaluator": {
            "overall_status": evaluation.get("overall_status"),
            "grounding_score": evaluation.get("grounding_score"),
            "completeness_score": evaluation.get("completeness_score"),
            "business_readiness_score": evaluation.get("business_readiness_score"),
            "unsupported_claims": evaluation.get("unsupported_claims", []),
        },
        "llm_usage": summarise_calls(calls),
    })

    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-set", type=Path, default=EVAL_SET)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument(
        "--only", nargs="*", default=None,
        help="Run only these item ids (for smoke-testing the runner).",
    )
    parser.add_argument(
        "--skip-flags", nargs="*", default=list(DEFAULT_SKIP_FLAGS),
        help="Skip items carrying any of these flags; they are logged separately.",
    )
    parser.add_argument("--label", type=str, default=None)
    parser.add_argument(
        "--min-call-interval", type=float, default=4.5,
        help="Seconds between LLM calls. The Gemini free tier allows 15 requests per "
             "minute; 4.5s keeps a paced run just under that. Set 0 to disable.",
    )
    args = parser.parse_args()

    if args.min_call_interval > 0:
        os.environ["LLM_MIN_INTERVAL_SECONDS"] = str(args.min_call_interval)
        print(f"pacing  : {args.min_call_interval}s between LLM calls "
              f"(free tier is 15 req/min)")

    eval_set = read_json(args.eval_set)
    items = eval_set["items"]

    if args.only:
        wanted = set(args.only)
        items = [i for i in items if i["id"] in wanted]
        missing = wanted - {i["id"] for i in items}
        if missing:
            raise SystemExit(f"unknown item ids: {sorted(missing)}")

    skip_flags = set(args.skip_flags)

    to_run = []
    skipped = []

    for it in items:
        hit = sorted(set(it["flags"]) & skip_flags)
        if hit:
            skipped.append({
                "id": it["id"], "question": it["question"], "flags": it["flags"],
                "skipped_because": hit,
                "reason": "E83 needs its poisoned Qdrant document upserted first; that is "
                          "a live mutation awaiting approval."
                          if "requires_fixture" in hit else "matched --skip-flags",
            })
        else:
            to_run.append(it)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"run_{timestamp}" + (f"_{args.label}" if args.label else "")
    output_root = args.results_dir / run_id / "artifacts"

    print(f"eval set : {args.eval_set}")
    print(f"items    : {len(to_run)} to run, {len(skipped)} skipped")
    print(f"run id   : {run_id}\n")

    results = []
    wall_started = time.perf_counter()

    for index, it in enumerate(to_run, start=1):
        print(f"[{index}/{len(to_run)}] {it['id']}: {it['question'][:66]}")
        rec = run_item(it, output_root)
        results.append(rec)

        if rec["outcome"] == "crashed":
            print(f"    CRASHED {rec['error']['type']}: {rec['error']['message'][:90]}")
        else:
            usage = rec["llm_usage"]["totals"]
            print(
                f"    {rec['outcome']:9s} route=sql:{rec['actual']['sql_intent']}"
                f"/graph:{rec['actual']['graph_intent']}"
                f" ev={rec['evidence']['sql_records']}/{rec['evidence']['graph_records']}"
                f"/{rec['evidence']['vector_records']}"
                f" grounding={rec['evaluator']['grounding_score']}"
                f" {rec['latency_ms']:.0f}ms"
                f" tok={usage['input_tokens']}in/{usage['output_tokens']}out"
            )

    wall_ms = round((time.perf_counter() - wall_started) * 1000.0, 1)

    totals = {
        "input_tokens": sum(r["llm_usage"]["totals"]["input_tokens"] for r in results),
        "output_tokens": sum(r["llm_usage"]["totals"]["output_tokens"] for r in results),
        "llm_calls": sum(r["llm_usage"]["totals"]["calls"] for r in results),
        "missing_usage_calls": sum(
            r["llm_usage"]["totals"]["missing_usage"] for r in results
        ),
        "rate_limit_retries": sum(r.get("rate_limit_retries", 0) for r in results),
    }
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]

    document = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "eval_set": str(args.eval_set.relative_to(PROJECT_ROOT)),
        "eval_set_schema_version": eval_set["schema_version"],
        "items_run": len(results),
        "items_skipped": len(skipped),
        "wall_clock_ms": wall_ms,
        "totals": totals,
        "skipped": skipped,
        "results": results,
    }

    out_path = args.results_dir / f"{run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    print(f"\nwall clock : {wall_ms/1000:.1f}s")
    print(f"tokens     : {totals['input_tokens']} in / {totals['output_tokens']} out "
          f"across {totals['llm_calls']} calls")
    if totals["missing_usage_calls"]:
        print(f"WARNING    : {totals['missing_usage_calls']} call(s) reported no usage metadata")
    print(f"results    : {out_path}")


if __name__ == "__main__":
    main()
