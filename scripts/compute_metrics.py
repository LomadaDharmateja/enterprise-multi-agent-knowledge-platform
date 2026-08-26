"""M3 metrics over an evaluation run.

Routing accuracy, refusal precision/recall and the deterministic assertion pass rates,
each with a 95% Wilson score interval.

Wilson, not the normal approximation, deliberately. At n = 16 per category and
proportions near 0 or 1 -- which is exactly where these will sit -- the normal
interval runs past 0 and 1 and understates uncertainty. Reporting +/- 1.96*sqrt(p(1-p)/n)
on 16/16 would give a confidence interval of zero width, which is not a measurement.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from eval_assertions import check_deterministic_assertions  # noqa: E402

EVAL_SET = PROJECT_ROOT / "tests" / "eval" / "eval_set_v1.json"


# --------------------------------------------------------------------------------
# Wilson score interval
# --------------------------------------------------------------------------------


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> dict:
    """95% Wilson score interval for a binomial proportion."""
    if total == 0:
        return {"n": 0, "k": 0, "point": None, "low": None, "high": None}

    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = (z / denominator) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))

    return {
        "n": total,
        "k": successes,
        "point": round(p, 4),
        "low": round(max(0.0, centre - half), 4),
        "high": round(min(1.0, centre + half), 4),
    }


def fmt(interval: dict) -> str:
    if interval["point"] is None:
        return "n=0  (no items)"

    return (
        f"{interval['k']}/{interval['n']} = {interval['point']*100:5.1f}%  "
        f"[{interval['low']*100:5.1f}, {interval['high']*100:5.1f}]"
    )


# --------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------


def leg_correct(required, permitted, actual) -> bool:
    """A leg is correct if it matches what the label requires, or is explicitly permitted.

    required=None means "this leg should not be selected" -- unless the label also
    permits it, which is how genuinely optional legs are expressed without either
    forcing or forbidding them.
    """
    if actual == required:
        return True

    if actual is not None and actual in (permitted or []):
        return True

    return False


def vector_correct(required: list, permitted: list, actual: list) -> bool:
    actual = actual or []

    # Every required group must be present.
    if not set(required or []).issubset(set(actual)):
        return False

    # Extra groups are fine only if the label permits them.
    allowed = set(required or []) | set(permitted or [])
    extras = set(actual) - allowed

    return not extras


def score_routing(item: dict, result: dict) -> dict | None:
    """Score the route. Returns None for items where no route is expected."""
    if not item["answerable"]:
        return None

    if result["outcome"] == "crashed":
        return {"sql": False, "graph": False, "vector": False, "overall": False,
                "note": "crashed"}

    required = item["expected_route"]["required"]
    permitted = item["expected_route"]["permitted"]
    actual = result["actual"]

    sql_ok = leg_correct(required["sql_intent"], permitted["sql_intent"], actual["sql_intent"])
    graph_ok = leg_correct(required["graph_intent"], permitted["graph_intent"], actual["graph_intent"])
    vec_ok = vector_correct(
        required["vector_artifact_groups"],
        permitted["vector_artifact_groups"],
        actual["vector_artifact_groups"],
    )

    return {
        "sql": sql_ok,
        "graph": graph_ok,
        "vector": vec_ok,
        "overall": sql_ok and graph_ok and vec_ok,
        "note": None,
    }


# --------------------------------------------------------------------------------
# Refusal
# --------------------------------------------------------------------------------


def refusal_confusion(items_by_id: dict, results: list) -> dict:
    """Confusion matrix over 'the system refused'.

    positive class = refused.
      TP  should refuse, did refuse
      FP  should answer, refused anyway
      FN  should refuse, answered anyway
      TN  should answer, answered
    """
    tp = fp = fn = tn = 0
    wrong_refusals = []
    missed_refusals = []

    for r in results:
        item = items_by_id[r["id"]]
        should_refuse = not item["answerable"]
        did_refuse = r.get("outcome") == "refused"

        if should_refuse and did_refuse:
            tp += 1
        elif not should_refuse and did_refuse:
            fp += 1
            wrong_refusals.append(r["id"])
        elif should_refuse and not did_refuse:
            fn += 1
            missed_refusals.append(r["id"])
        else:
            tn += 1

    return {
        "true_positive": tp, "false_positive": fp,
        "false_negative": fn, "true_negative": tn,
        "precision": wilson(tp, tp + fp),
        "recall": wilson(tp, tp + fn),
        "refused_an_answerable_question": wrong_refusals,
        "answered_an_unanswerable_question": missed_refusals,
    }


# --------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------


def compute(run: dict, eval_set: dict) -> dict:
    items_by_id = {i["id"]: i for i in eval_set["items"]}
    results = run["results"]

    routing_rows = []
    assertion_rows = []
    unexpected = []

    for r in results:
        item = items_by_id[r["id"]]

        routing = score_routing(item, r)

        if routing is not None:
            routing_rows.append({"id": r["id"], "category": item["category"], **routing})

        for a in check_deterministic_assertions(item, r):
            assertion_rows.append(a.as_dict() | {"category": item["category"]})

        if r["outcome"] == "crashed":
            unexpected.append({"id": r["id"], "what": "crashed",
                               "detail": r["error"]["message"][:160]})
        elif item["answerable"] and r["outcome"] == "refused":
            unexpected.append({"id": r["id"], "what": "refused an answerable question",
                               "detail": r["actual"]["refusal_reason"]})
        elif not item["answerable"] and r["outcome"] == "completed":
            unexpected.append({"id": r["id"], "what": "answered an unanswerable question",
                               "detail": (r["answer_text"] or "")[:160]})

    # routing by category
    categories = sorted({row["category"] for row in routing_rows})
    routing_by_category = {
        cat: wilson(
            sum(1 for row in routing_rows if row["category"] == cat and row["overall"]),
            sum(1 for row in routing_rows if row["category"] == cat),
        )
        for cat in categories
    }

    routing_overall = wilson(
        sum(1 for row in routing_rows if row["overall"]), len(routing_rows)
    )
    routing_by_leg = {
        leg: wilson(sum(1 for row in routing_rows if row[leg]), len(routing_rows))
        for leg in ("sql", "graph", "vector")
    }

    # assertions
    assertion_types = sorted({a["assertion"] for a in assertion_rows})
    assertions_by_type = {}

    for atype in assertion_types:
        rows = [a for a in assertion_rows if a["assertion"] == atype]
        evaluated = [a for a in rows if a["status"] in ("pass", "fail")]
        assertions_by_type[atype] = {
            "interval": wilson(sum(1 for a in evaluated if a["status"] == "pass"), len(evaluated)),
            "inconclusive": sum(1 for a in rows if a["status"] == "inconclusive"),
            "failures": [a["item_id"] for a in rows if a["status"] == "fail"],
        }

    evaluated_all = [a for a in assertion_rows if a["status"] in ("pass", "fail")]
    assertions_overall = wilson(
        sum(1 for a in evaluated_all if a["status"] == "pass"), len(evaluated_all)
    )

    # evaluator score distribution, over items that produced an answer
    scored = [
        r for r in results
        if r["outcome"] == "completed" and r["evaluator"]["grounding_score"] is not None
    ]
    distribution = {}

    for field in ("grounding_score", "completeness_score", "business_readiness_score"):
        counts: dict[str, int] = {}

        for r in scored:
            counts[str(r["evaluator"][field])] = counts.get(str(r["evaluator"][field]), 0) + 1

        distribution[field] = dict(sorted(counts.items()))

    return {
        "run_id": run["run_id"],
        "items_run": len(results),
        "routing": {
            "overall": routing_overall,
            "by_leg": routing_by_leg,
            "by_category": routing_by_category,
            "scored_items": len(routing_rows),
            "not_scored_refusal_expected": len(results) - len(routing_rows),
            "rows": routing_rows,
        },
        "refusal": refusal_confusion(items_by_id, results),
        "assertions": {
            "overall": assertions_overall,
            "by_type": assertions_by_type,
            "rows": assertion_rows,
        },
        "evaluator_score_distribution": {
            "items_scored": len(scored),
            "items_without_a_score": len(results) - len(scored),
            **distribution,
        },
        "unexpected_behaviour": unexpected,
    }


def render(report: dict) -> str:
    lines = [
        f"run: {report['run_id']}   items: {report['items_run']}",
        "",
        "ROUTING ACCURACY (95% Wilson)",
        f"  overall            {fmt(report['routing']['overall'])}",
    ]

    for leg, interval in report["routing"]["by_leg"].items():
        lines.append(f"    {leg:16s} {fmt(interval)}")

    lines.append("  by category")

    for cat, interval in report["routing"]["by_category"].items():
        lines.append(f"    {cat:16s} {fmt(interval)}")

    lines.append(
        f"  ({report['routing']['not_scored_refusal_expected']} refusal-expected items "
        "carry no route and are excluded)"
    )

    ref = report["refusal"]
    lines += [
        "",
        "REFUSAL (positive class = refused)",
        f"  precision          {fmt(ref['precision'])}",
        f"  recall             {fmt(ref['recall'])}",
        f"  confusion          TP={ref['true_positive']} FP={ref['false_positive']} "
        f"FN={ref['false_negative']} TN={ref['true_negative']}",
    ]

    if ref["refused_an_answerable_question"]:
        lines.append(f"  refused answerable : {ref['refused_an_answerable_question']}")

    if ref["answered_an_unanswerable_question"]:
        lines.append(f"  answered unanswerable: {ref['answered_an_unanswerable_question']}")

    lines += ["", "DETERMINISTIC ASSERTIONS (95% Wilson, over evaluated only)",
              f"  overall            {fmt(report['assertions']['overall'])}"]

    for atype, block in report["assertions"]["by_type"].items():
        line = f"    {atype:32s} {fmt(block['interval'])}"

        if block["inconclusive"]:
            line += f"   +{block['inconclusive']} inconclusive"

        lines.append(line)

        if block["failures"]:
            lines.append(f"        failures: {block['failures']}")

    dist = report["evaluator_score_distribution"]
    lines += ["", "EVALUATOR SCORE DISTRIBUTION",
              f"  scored {dist['items_scored']} item(s); "
              f"{dist['items_without_a_score']} produced no score (refusals and crashes)"]

    for field in ("grounding_score", "completeness_score", "business_readiness_score"):
        lines.append(f"    {field:26s} {dist.get(field)}")

    if report["unexpected_behaviour"]:
        lines += ["", "UNEXPECTED BEHAVIOUR"]

        for u in report["unexpected_behaviour"]:
            lines.append(f"  {u['id']}: {u['what']} -- {u['detail']}")
    else:
        lines += ["", "UNEXPECTED BEHAVIOUR: none"]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="a run_*.json produced by run_eval.py")
    parser.add_argument("--eval-set", type=Path, default=EVAL_SET)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    run = json.loads(args.run.read_text(encoding="utf-8"))
    eval_set = json.loads(args.eval_set.read_text(encoding="utf-8"))

    report = compute(run, eval_set)
    print(render(report))

    out = args.out or args.run.with_name(args.run.stem + "_metrics.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nmetrics: {out}")


if __name__ == "__main__":
    main()
