"""M2 task 5: re-run the frozen baseline questions and diff against M0.

Reads tests/baseline/baseline_results.json, re-runs every question through the current
workflow, and reports route, evidence counts, answer length and evaluator score side by
side with the frozen values.

Nothing here asserts. The M2 evidence is expected to differ -- that is the milestone.
The job of this script is to make the difference visible and attributable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

for directory in (PROJECT_ROOT / "src" / "orchestration",):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from agentic_workflow import run_agentic_workflow  # noqa: E402

BASELINE_PATH = PROJECT_ROOT / "tests" / "baseline" / "baseline_results.json"


def evidence_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def run_one(question: dict, output_root: Path) -> dict:
    question_id = question["id"]
    query = question["query"]

    result: dict = {
        "id": question_id,
        "group": question["group"],
        "query": query,
        "baseline": {
            "outcome": question["outcome"],
            "sql_intent": (question.get("route") or {}).get("sql_intent"),
            "graph_intent": (question.get("route") or {}).get("graph_intent"),
            "vector_artifact_groups": (question.get("route") or {}).get(
                "vector_artifact_groups"
            ),
            "exception_type": question.get("exception_type"),
            "exception_message": question.get("exception_message"),
        },
    }

    try:
        final = run_agentic_workflow(
            query=query,
            output_dir=output_root / question_id,
        )
    except Exception as exc:  # noqa: BLE001 -- a crash is a result worth recording
        result["m2"] = {
            "outcome": "crashed",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback_tail": traceback.format_exc().strip().splitlines()[-1],
        }
        return result

    route = final.get("planned_route") or {}
    sources = final.get("source_summary") or {}
    evaluation = final.get("evaluation_summary") or {}

    raw_path = (final.get("output_files") or {}).get("raw_retrieval")
    sql_records: list = []
    graph_records: list = []

    if raw_path and Path(raw_path).exists():
        raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        sql_records = raw["retrieval_results"]["sql"]["records"]
        graph_records = raw["retrieval_results"]["graph"]["records"]

    result["m2"] = {
        "outcome": "refused" if final.get("answerable") is False else "completed",
        "overall_status": final.get("overall_status"),
        "answerable": final.get("answerable", True),
        "refusal_reason": final.get("refusal_reason"),
        "sql_intent": route.get("sql_intent"),
        "graph_intent": route.get("graph_intent"),
        "vector_artifact_groups": route.get("vector_artifact_groups"),
        "sql_filters": (route.get("sql_plan") or {}).get("filters"),
        "sql_sort_by": (route.get("sql_plan") or {}).get("sort_by"),
        "graph_filters": (route.get("graph_plan") or {}).get("filters"),
        "dropped_filters": route.get("dropped_filters"),
        "sql_records": (sources.get("sql") or {}).get("records", 0),
        "graph_records": (sources.get("graph") or {}).get("records", 0),
        "vector_records": (sources.get("vector") or {}).get("records", 0),
        "sql_evidence_hash": evidence_hash(sql_records),
        "graph_evidence_hash": evidence_hash(graph_records),
        "answer_length_chars": final.get("answer_length_chars"),
        "grounding_score": evaluation.get("grounding_score"),
        "completeness_score": evaluation.get("completeness_score"),
        "business_readiness_score": evaluation.get("business_readiness_score"),
        "evidence_quality": final.get("evidence_quality"),
    }

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "reports" / "m2_baseline",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "tests" / "baseline" / "m2_baseline_comparison.json",
    )
    args = parser.parse_args()

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    results = []

    for question in baseline["questions"]:
        print(f"\n=== {question['id']}: {question['query'][:70]}")
        outcome = run_one(question, args.output_root)
        results.append(outcome)

        m2 = outcome["m2"]
        print(f"    baseline outcome : {outcome['baseline']['outcome']}")
        print(f"    m2 outcome       : {m2.get('outcome')}")
        print(
            "    route            : "
            f"sql={m2.get('sql_intent')} graph={m2.get('graph_intent')} "
            f"vec={m2.get('vector_artifact_groups')}"
        )
        print(
            "    records          : "
            f"sql={m2.get('sql_records')} graph={m2.get('graph_records')} "
            f"vector={m2.get('vector_records')}"
        )
        print(f"    answer chars     : {m2.get('answer_length_chars')}")
        print(
            "    scores           : "
            f"grounding={m2.get('grounding_score')} "
            f"completeness={m2.get('completeness_score')} "
            f"readiness={m2.get('business_readiness_score')}"
        )

    distinct_sql = {
        r["m2"].get("sql_evidence_hash")
        for r in results
        if r["m2"].get("sql_evidence_hash")
    }
    distinct_graph = {
        r["m2"].get("graph_evidence_hash")
        for r in results
        if r["m2"].get("graph_evidence_hash")
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "milestone": "M2",
        "baseline_milestone": baseline["milestone"],
        "questions": results,
        "cross_question": {
            "questions_run": len(results),
            "distinct_sql_evidence_hashes": len(distinct_sql),
            "distinct_graph_evidence_hashes": len(distinct_graph),
            "baseline_distinct_sql_evidence_hashes": baseline["cross_question"][
                "distinct_sql_evidence_hashes"
            ],
            "baseline_distinct_graph_evidence_hashes": baseline["cross_question"][
                "distinct_graph_evidence_hashes"
            ],
        },
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)
    print(f"distinct SQL evidence sets   : {len(distinct_sql)} (M0: "
          f"{report['cross_question']['baseline_distinct_sql_evidence_hashes']})")
    print(f"distinct graph evidence sets : {len(distinct_graph)} (M0: "
          f"{report['cross_question']['baseline_distinct_graph_evidence_hashes']})")
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
