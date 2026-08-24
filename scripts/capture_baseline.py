"""Capture the M0 frozen baseline: what the system does today, before anything changes.

Every later milestone is scored against tests/baseline/baseline_results.json. It is
never deleted and never regenerated in place -- if a milestone changes behaviour, the
change shows up as a diff against this file, which is the whole point.

Ten questions in three groups:

  five_validation_cases  the five cases in agentic_workflow_validator.py. They are
                         byte-identical to the five worked examples embedded in the
                         planner's own prompt (AUDIT.md P3), so a pass here measures
                         memorisation, not routing skill. Recorded because it is the
                         number the project currently claims.
  document_paraphrases   the three phrasings PROJECT_SUMMARY.md section 3 offers as
                         the reason an LLM planner is needed. The planner scored 1/3
                         on these at audit time.
  controls               "purple monkey dishwasher" (no valid answer exists; F-03
                         says it returns the same evidence as the flagship question)
                         and "Which seller had the highest revenue?" (crashes,
                         because validate_and_normalize_plan requires a non-null
                         graph intent -- gemini_query_planner.py:201).

For each question the script records the route the planner chose, sha256 hashes of
the evidence each retrieval leg returned, the answer and its hash, the evaluator's
verdict, and -- where the workflow raises -- the exception type and message.

Requires the live stack and a working GEMINI_API_KEY. Costs ~10 planner calls,
~10 answer calls and ~10 evaluator calls.

    python scripts/capture_baseline.py
    python scripts/capture_baseline.py --only seller_complaint_warranty_policy
    python scripts/capture_baseline.py --output tests/baseline/baseline_results.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _sub in ("orchestration", "retrieval", "generation", "evaluation", "planning", "observability"):
    sys.path.insert(0, str(PROJECT_ROOT / "src" / _sub))

from agentic_workflow import run_agentic_workflow  # noqa: E402

DEFAULT_OUTPUT = PROJECT_ROOT / "tests" / "baseline" / "baseline_results.json"
DEFAULT_ARTIFACT_DIR = PROJECT_ROOT / "reports" / "baseline_m0"
MAX_TRANSIENT_RETRIES = 3

QUESTIONS: list[dict[str, Any]] = [
    # --- the five cases the project currently counts as its evidence -------------
    {
        "id": "seller_complaint_warranty_policy",
        "group": "five_validation_cases",
        "query": "Find sellers with negative customer complaints, warranty issues, and relevant support policies",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": "warranty_product_seller_paths",
        "expected_vector_groups": ["support_tickets", "warranty_claims", "policy_documents"],
        "note": "verbatim few-shot example in build_planner_prompt (gemini_query_planner.py:50-172)",
    },
    {
        "id": "late_delivery_logistics_region_guidance",
        "group": "five_validation_cases",
        "query": "Investigate late delivery logistics incidents by customer region and find troubleshooting guidance",
        "expected_sql_intent": "order_summary",
        "expected_graph_intent": "logistics_region_paths",
        "expected_vector_groups": ["logistics_incidents", "troubleshooting_guides"],
        "note": "verbatim few-shot example",
    },
    {
        "id": "payment_refund_policy",
        "group": "five_validation_cases",
        "query": "Find payment questions, refund policies, and customer support cases",
        "expected_sql_intent": "payment_summary",
        "expected_graph_intent": "customer_ticket_order_product_paths",
        "expected_vector_groups": ["support_tickets", "policy_documents"],
        "note": "verbatim few-shot example",
    },
    {
        "id": "product_quality_troubleshooting",
        "group": "five_validation_cases",
        "query": "Investigate product quality complaints and find troubleshooting procedures",
        "expected_sql_intent": "review_intelligence",
        "expected_graph_intent": "customer_ticket_order_product_paths",
        "expected_vector_groups": ["support_tickets", "troubleshooting_guides"],
        "note": "verbatim few-shot example",
    },
    {
        "id": "seller_negative_ticket_paths",
        "group": "five_validation_cases",
        "query": "Show seller ticket paths for negative complaints and product issues",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": "seller_ticket_product_paths",
        "expected_vector_groups": ["support_tickets"],
        "note": "verbatim few-shot example",
    },
    # --- the paraphrases the project's own summary uses to justify a planner -----
    {
        "id": "paraphrase_most_complaints",
        "group": "document_paraphrases",
        "query": "Which sellers have the most complaints?",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": "warranty_product_seller_paths",
        "expected_vector_groups": ["support_tickets"],
        "note": "PROJECT_SUMMARY.md section 3; audit result was SQL MISS / GRAPH MISS",
    },
    {
        "id": "paraphrase_vendors_poor_experience",
        "group": "document_paraphrases",
        "query": "Find vendors connected to poor customer experiences.",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": "warranty_product_seller_paths",
        "expected_vector_groups": ["support_tickets"],
        "note": "PROJECT_SUMMARY.md section 3; audit result was SQL HIT / GRAPH MISS",
    },
    {
        "id": "paraphrase_suppliers_negative_reviews",
        "group": "document_paraphrases",
        "query": "Show suppliers associated with negative reviews and warranty issues.",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": "warranty_product_seller_paths",
        "expected_vector_groups": ["support_tickets", "warranty_claims"],
        "note": "PROJECT_SUMMARY.md section 3; audit result was SQL HIT / GRAPH HIT",
    },
    # --- controls ---------------------------------------------------------------
    {
        "id": "control_purple_monkey_dishwasher",
        "group": "controls",
        "query": "purple monkey dishwasher",
        "expected_sql_intent": None,
        "expected_graph_intent": None,
        "expected_vector_groups": None,
        "note": (
            "No valid answer exists. AUDIT.md F-03: returns byte-identical SQL and "
            "graph evidence to the flagship question, because no template binds "
            "anything but :limit. The correct future behaviour is a refusal."
        ),
    },
    {
        "id": "control_highest_revenue_seller",
        "group": "controls",
        "query": "Which seller had the highest revenue?",
        "expected_sql_intent": "seller_performance",
        "expected_graph_intent": None,
        "expected_vector_groups": None,
        "note": (
            "The project's own worked example. Expected to CRASH: a SQL-only plan is "
            "unrepresentable because validate_and_normalize_plan requires a non-null "
            "graph intent (gemini_query_planner.py:201)."
        ),
    },
]


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str))


def evidence_hashes(raw_retrieval: dict[str, Any]) -> dict[str, Any]:
    """Hash each retrieval leg separately.

    Separate hashes are what make F-03 measurable: if two different questions produce
    the same sql_records_sha256, the SQL leg is not responding to the question.
    """
    results = raw_retrieval.get("retrieval_results", {})
    sql = results.get("sql", {})
    graph = results.get("graph", {})
    vector = results.get("vector", {})

    vector_groups = vector.get("results_by_artifact_group", {}) or {}

    def _sorted_hash(records: list[Any]) -> str:
        """Order-independent hash.

        Compared against records_sha256 this separates "a different set of rows came
        back" from "the same rows came back in a different order" -- several templates
        have non-unique ORDER BY clauses, so the row order is not stable between runs.
        """
        return sha256_json(sorted(json.dumps(r, sort_keys=True, default=str) for r in records))

    return {
        "sql": {
            "intent": sql.get("intent"),
            "record_count": sql.get("record_count"),
            "records_sha256": sha256_json(sql.get("records", [])),
            "records_sorted_sha256": _sorted_hash(sql.get("records", []) or []),
        },
        "graph": {
            "intent": graph.get("intent"),
            "record_count": graph.get("record_count"),
            "records_sha256": sha256_json(graph.get("records", [])),
            "records_sorted_sha256": _sorted_hash(graph.get("records", []) or []),
        },
        "vector": {
            "artifact_groups": vector.get("artifact_groups"),
            "record_count": vector.get("record_count"),
            "results_sha256": sha256_json(vector_groups),
            "per_group_sha256": {
                name: sha256_json(payload) for name, payload in sorted(vector_groups.items())
            },
        },
    }


def run_one(question: dict[str, Any], artifact_dir: Path) -> dict[str, Any]:
    case_dir = artifact_dir / question["id"]
    record: dict[str, Any] = {
        "id": question["id"],
        "group": question["group"],
        "query": question["query"],
        "expected_sql_intent": question["expected_sql_intent"],
        "expected_graph_intent": question["expected_graph_intent"],
        "expected_vector_groups": question["expected_vector_groups"],
        "note": question["note"],
        "artifact_dir": _relative(case_dir),
    }

    started = time.perf_counter()
    transient_attempts = 0
    while True:
        try:
            response = run_agentic_workflow(query=question["query"], output_dir=case_dir)
            record["outcome"] = "completed"
            break
        except Exception as exc:  # noqa: BLE001 -- a crash is a baseline observation
            # A 5xx from Gemini is infrastructure noise, not system behaviour. Retry a
            # few times so the baseline records what the system does, not what the
            # provider was doing that minute. Anything else is recorded as-is.
            message = str(exc)
            transient = "503" in message or "UNAVAILABLE" in message or "429" in message
            if transient and transient_attempts < MAX_TRANSIENT_RETRIES:
                transient_attempts += 1
                print(f"      transient provider error, retry {transient_attempts}: {message[:70]}")
                time.sleep(20 * transient_attempts)
                continue
            record["outcome"] = "crashed"
            record["exception_type"] = type(exc).__name__
            record["exception_message"] = message
            record["traceback_tail"] = traceback.format_exc().strip().splitlines()[-1]
            record["transient_retries"] = transient_attempts
            record["elapsed_seconds"] = round(time.perf_counter() - started, 2)
            return record

    record["transient_retries"] = transient_attempts

    record["elapsed_seconds"] = round(time.perf_counter() - started, 2)

    raw_path = case_dir / "workflow_raw_retrieval.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {}
    plan = raw.get("route_plan", {})

    record["route"] = {
        "sql_intent": plan.get("sql_intent"),
        "graph_intent": plan.get("graph_intent"),
        "vector_artifact_groups": plan.get("vector_artifact_groups"),
        "planner_model": plan.get("model"),
    }
    record["route_matches_expected"] = {
        "sql": plan.get("sql_intent") == question["expected_sql_intent"],
        "graph": plan.get("graph_intent") == question["expected_graph_intent"],
        "vector": (
            sorted(plan.get("vector_artifact_groups") or [])
            == sorted(question["expected_vector_groups"] or [])
            if question["expected_vector_groups"] is not None
            else None
        ),
    }
    record["evidence"] = evidence_hashes(raw)

    context_path = case_dir / "workflow_retrieval_context.json"
    if context_path.exists():
        record["retrieval_context_sha256"] = sha256_text(
            context_path.read_text(encoding="utf-8")
        )

    answer_path = case_dir / "workflow_answer.md"
    if answer_path.exists():
        answer = answer_path.read_text(encoding="utf-8")
        record["answer"] = {
            "length_chars": len(answer),
            "sha256": sha256_text(answer),
            "text": answer,
        }

    evaluation = response.get("evaluation_summary", {}) or {}
    record["evaluation"] = {
        "overall_status": evaluation.get("overall_status"),
        "evaluator_status": evaluation.get("evaluator_status"),
        "grounding_score": evaluation.get("grounding_score"),
        "completeness_score": evaluation.get("completeness_score"),
        "business_readiness_score": evaluation.get("business_readiness_score"),
        "sql_evidence_used": evaluation.get("sql_evidence_used"),
        "graph_evidence_used": evaluation.get("graph_evidence_used"),
        "document_evidence_used": evaluation.get("document_evidence_used"),
        "limitations_mentioned": evaluation.get("limitations_mentioned"),
        "unsupported_claims": evaluation.get("unsupported_claims"),
        "missing_evidence": evaluation.get("missing_evidence"),
        "deterministic_failures": evaluation.get("deterministic_failures"),
        "raw_sha256": sha256_json(evaluation),
    }
    record["reported_overall_status"] = response.get("overall_status")
    return record


def cross_question_findings(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Facts that only exist across questions -- chiefly F-03."""
    by_hash: dict[tuple[str, str], list[str]] = {}
    for record in records:
        evidence = record.get("evidence")
        if not evidence:
            continue
        key = (evidence["sql"]["records_sha256"], evidence["graph"]["records_sha256"])
        by_hash.setdefault(key, []).append(record["id"])

    collisions = {
        f"sql={sql[:16]} graph={graph[:16]}": ids
        for (sql, graph), ids in by_hash.items()
        if len(ids) > 1
    }
    completed = [r for r in records if r.get("evidence")]

    def _distinct(leg: str, field: str) -> int:
        return len({r["evidence"][leg][field] for r in completed})

    # Group by the intent the planner chose. If a template responded to the question
    # rather than only to the intent, two questions on the same intent would produce
    # different evidence. They do not: that is F-03.
    by_intent: dict[str, set[str]] = {}
    for record in completed:
        by_intent.setdefault(
            record["evidence"]["sql"]["intent"], set()
        ).add(record["evidence"]["sql"]["records_sorted_sha256"])

    return {
        "questions_completed": len(completed),
        "distinct_sql_evidence_hashes": _distinct("sql", "records_sha256"),
        "distinct_sql_evidence_hashes_order_independent": _distinct(
            "sql", "records_sorted_sha256"
        ),
        "distinct_graph_evidence_hashes": _distinct("graph", "records_sha256"),
        "distinct_graph_evidence_hashes_order_independent": _distinct(
            "graph", "records_sorted_sha256"
        ),
        "sql_intents_used": len(by_intent),
        "sql_evidence_sets_per_intent": {
            intent: len(hashes) for intent, hashes in sorted(by_intent.items())
        },
        "identical_evidence_groups": collisions,
        "f03_note": (
            "identical_evidence_groups lists questions whose SQL and graph evidence is "
            "byte-identical despite the questions differing. That is F-03: no template "
            "binds anything but :limit, so the evidence answers the intent, not the "
            "question. M2's exit criterion is that this dict becomes empty."
        ),
        "instability_note": (
            "sql_evidence_sets_per_intent greater than 1 does NOT mean a template "
            "responded to the question. It means the template is not reproducible: "
            "review_intelligence orders by review_score alone, 11,424 rows tie at "
            "score 1, and LIMIT 10 takes an arbitrary ten. Run "
            "scripts/check_template_determinism.py to measure it."
        ),
    }


def environment() -> dict[str, Any]:
    def _cmd(*args: str) -> str | None:
        try:
            return subprocess.run(
                args, cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return None

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _cmd("git", "rev-parse", "HEAD"),
        "git_dirty": bool(_cmd("git", "status", "--porcelain")),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the M0 frozen baseline.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    parser.add_argument("--only", action="append", default=None, help="question id; repeatable")
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    selected = [q for q in QUESTIONS if not args.only or q["id"] in args.only]
    records = []
    for index, question in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] {question['id']}: {question['query'][:70]}")
        record = run_one(question, artifact_dir)
        outcome = record["outcome"]
        if outcome == "crashed":
            print(f"      CRASHED  {record['exception_type']}: {record['exception_message'][:90]}")
        else:
            route = record["route"]
            match = record["route_matches_expected"]
            print(
                f"      sql={route['sql_intent']} ({'HIT' if match['sql'] else 'MISS'})  "
                f"graph={route['graph_intent']} ({'HIT' if match['graph'] else 'MISS'})  "
                f"answer={record.get('answer', {}).get('length_chars')}ch  "
                f"eval={record['evaluation']['overall_status']}"
                f"(g={record['evaluation']['grounding_score']})  {record['elapsed_seconds']}s"
            )
        records.append(record)

    baseline = {
        "schema_version": 1,
        "milestone": "M0",
        "purpose": (
            "Frozen record of system behaviour before any rebuild work. Never delete "
            "this file. Later milestones are measured as a diff against it."
        ),
        "environment": environment(),
        "cross_question": cross_question_findings(records),
        "questions": records,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print(json.dumps(baseline["cross_question"], indent=2))
    print(f"\nbaseline written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
