"""M3 single-agent baseline: one prompt, all three databases, no planner.

The ablation the rebuild plan asks for. If the multi-agent pipeline does not beat this,
that is a finding worth publishing rather than a failure to hide.

What is removed relative to the multi-agent pipeline:

  - the Gemini planner        -> routes come from the pre-existing keyword detectors
                                 (`detect_sql_intent` / `detect_graph_intent` /
                                 `detect_vector_filters`), which is what "no planner"
                                 means for a system that must still pick a table
  - parameter binding         -> no filters, no sort key; templates run at their defaults
  - evidence-linked filtering -> off
  - the separate evaluator    -> no judge call
  - the refusal path          -> a single agent has no plan to mark unanswerable

What is kept identical: the same databases, the same six SQL templates, the same five
Cypher templates, the same Qdrant collection and embedding model, the same context
compaction, and the same 82 questions with the same labels.

One Gemini call per item.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

for _d in (
    PROJECT_ROOT / "src" / "retrieval",
    PROJECT_ROOT / "src" / "generation",
    PROJECT_ROOT / "src" / "observability",
    PROJECT_ROOT / "scripts",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from hybrid_retriever import (  # noqa: E402
    build_neo4j_driver,
    build_postgres_engine,
    build_qdrant_client,
    detect_graph_intent,
    detect_sql_intent,
    detect_vector_filters,
    graph_retrieve,
    load_settings,
    sql_retrieve,
    vector_retrieve,
)
from llm_usage import collect, timed_call  # noqa: E402
from retrieval_context_builder import (  # noqa: E402
    build_document_evidence,
    build_graph_evidence,
    build_sql_evidence,
)

EVAL_SET = PROJECT_ROOT / "tests" / "eval" / "eval_set_v1.json"
RESULTS_DIR = PROJECT_ROOT / "tests" / "eval" / "results"


PROMPT = """You are an enterprise business analyst with access to three databases.

Answer the business question using ONLY the evidence below. Do not use outside
knowledge. If the evidence does not support an answer, say so plainly rather than
guessing. State any limitations of the evidence.

Business question:
{question}

PostgreSQL evidence (structured facts) -- intent `{sql_intent}`:
{sql_evidence}

Neo4j evidence (entity relationships) -- intent `{graph_intent}`:
{graph_evidence}

Qdrant evidence (retrieved documents) -- groups {vector_groups}:
{document_evidence}

Write a grounded business answer. Cite the specific entity IDs, record values and
document identifiers you used.
"""


def compact(value, limit=7000):
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit].strip() + "\n...TRUNCATED..."


def call_gemini(prompt: str, model: str, api_key: str) -> str:
    from google import genai

    client = genai.Client(api_key=api_key)

    with timed_call("single_agent", model, len(prompt)) as call:
        response = client.models.generate_content(model=model, contents=prompt)
        call["response"] = response

    text = getattr(response, "text", None)

    if not text or not text.strip():
        raise RuntimeError("Gemini returned an empty response.")

    return text.strip()


def run_item(item, engine, driver, qdrant, model, settings, api_key, max_retries=4):
    qid = item["id"]
    question = item["question"]

    record = {
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

    # --- routing without a planner: the keyword detectors that predate the LLM
    sql_intent = detect_sql_intent(question)
    graph_intent = detect_graph_intent(question)
    vector_groups = detect_vector_filters(question)

    calls: list = []

    for attempt in range(max_retries + 1):
        try:
            with collect() as calls:
                sql_results = sql_retrieve(
                    engine=engine, query=question, limit=10,
                    forced_intent=sql_intent, filters={}, sort_by=None,
                )
                graph_results = graph_retrieve(
                    driver=driver, query=question, limit=10,
                    forced_intent=graph_intent, filters={},
                )
                vector_results = vector_retrieve(
                    client=qdrant, model=model, collection_name=settings["qdrant_collection"],
                    query=question, per_group_limit=5,
                    forced_artifact_groups=vector_groups,
                )

                sql_evidence = build_sql_evidence(sql_results, max_records=5)
                graph_evidence = build_graph_evidence(graph_results, max_records=5)
                document_evidence = build_document_evidence(vector_results, max_records_per_group=5)

                prompt = PROMPT.format(
                    question=question,
                    sql_intent=sql_intent,
                    graph_intent=graph_intent,
                    vector_groups=vector_groups,
                    sql_evidence=compact(sql_evidence),
                    graph_evidence=compact(graph_evidence),
                    document_evidence=compact(document_evidence, 9000),
                )

                answer = call_gemini(prompt, settings["gemini_model"], api_key)
            break
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            retryable = "429" in message or "RESOURCE_EXHAUSTED" in message

            if retryable and attempt < max_retries:
                import re as _re

                hint = _re.search(r"retry in ([0-9.]+)s", message, _re.I)
                delay = float(hint.group(1)) + 2.0 if hint else min(60.0, 5.0 * 2 ** attempt)
                print(f"    rate limited; sleeping {delay:.0f}s")
                time.sleep(delay)
                continue

            record["outcome"] = "crashed"
            record["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            record["error"] = {"type": type(exc).__name__, "message": message,
                               "traceback_tail": traceback.format_exc().strip().splitlines()[-1]}
            record["llm_usage"] = {"totals": {"calls": 0, "input_tokens": 0,
                                              "output_tokens": 0, "missing_usage": 0}}
            return record

    vector_records = []
    for group in vector_results["results_by_artifact_group"].values():
        vector_records.extend(group)

    totals = {
        "calls": len(calls),
        "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
        "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
        "missing_usage": sum(1 for c in calls if c["input_tokens"] is None),
    }

    record.update({
        "outcome": "completed",
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "actual": {
            "answerable": True,       # a single agent has no refusal mechanism
            "refusal_reason": None,
            "sql_intent": sql_intent,
            "graph_intent": graph_intent,
            "vector_artifact_groups": vector_groups,
            "sql_filters": {},
            "sql_sort_by": None,
            "graph_filters": {},
            "dropped_filters": [],
        },
        "evidence": {
            "sql_records": sql_results["record_count"],
            "graph_records": graph_results["record_count"],
            "vector_records": vector_results["record_count"],
            "sql_record_sample": sql_results["records"][:10],
            "graph_record_sample": graph_results["records"][:10],
            "vector_record_sample": vector_records[:10],
        },
        "evidence_quality": None,
        "answer_text": answer,
        "answer_length_chars": len(answer),
        "evaluator": {
            "overall_status": None, "grounding_score": None,
            "completeness_score": None, "business_readiness_score": None,
            "unsupported_claims": [],
        },
        "llm_usage": {"totals": totals, "by_agent": {"single_agent": totals}, "calls": calls},
    })

    return record


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eval-set", type=Path, default=EVAL_SET)
    ap.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--min-call-interval", type=float, default=4.5)
    args = ap.parse_args()

    if args.min_call_interval > 0:
        os.environ["LLM_MIN_INTERVAL_SECONDS"] = str(args.min_call_interval)

    eval_set = json.loads(args.eval_set.read_text(encoding="utf-8"))
    items = [i for i in eval_set["items"] if "requires_fixture" not in i["flags"]]

    if args.only:
        items = [i for i in items if i["id"] in set(args.only)]

    settings = load_settings()
    settings["gemini_model"] = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    api_key = os.getenv("GEMINI_API_KEY")

    engine = build_postgres_engine(settings)
    driver = build_neo4j_driver(settings)
    qdrant = build_qdrant_client(settings)

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        settings["embedding_model_name"], revision=settings["embedding_model_revision"]
    )

    print(f"single-agent baseline: {len(items)} items, 1 LLM call each\n")

    results = []
    started = time.perf_counter()

    try:
        for index, item in enumerate(items, start=1):
            print(f"[{index}/{len(items)}] {item['id']}: {item['question'][:60]}")
            record = run_item(item, engine, driver, qdrant, model, settings, api_key)
            results.append(record)

            if record["outcome"] == "crashed":
                print(f"    CRASHED {record['error']['type']}")
            else:
                print(f"    route=sql:{record['actual']['sql_intent']}"
                      f"/graph:{record['actual']['graph_intent']} "
                      f"ev={record['evidence']['sql_records']}"
                      f"/{record['evidence']['graph_records']}"
                      f"/{record['evidence']['vector_records']} "
                      f"{record['latency_ms']:.0f}ms")
    finally:
        engine.dispose()
        driver.close()

    wall = round((time.perf_counter() - started) * 1000.0, 1)
    totals = {
        "input_tokens": sum(r["llm_usage"]["totals"]["input_tokens"] for r in results),
        "output_tokens": sum(r["llm_usage"]["totals"]["output_tokens"] for r in results),
        "llm_calls": sum(r["llm_usage"]["totals"]["calls"] for r in results),
        "missing_usage_calls": sum(r["llm_usage"]["totals"]["missing_usage"] for r in results),
    }
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]

    run_id = "baseline_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.results_dir / f"{run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system": "single_agent_baseline",
        "eval_set": str(args.eval_set.relative_to(PROJECT_ROOT)),
        "eval_set_schema_version": eval_set["schema_version"],
        "items_run": len(results),
        "items_skipped": 0,
        "wall_clock_ms": wall,
        "totals": totals,
        "skipped": [],
        "results": results,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"\nwall clock : {wall/1000:.1f}s")
    print(f"tokens     : {totals['input_tokens']} in / {totals['output_tokens']} out "
          f"across {totals['llm_calls']} calls")
    print(f"results    : {out}")


if __name__ == "__main__":
    main()
