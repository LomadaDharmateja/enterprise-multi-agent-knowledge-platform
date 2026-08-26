"""Render the Step 4 raw-results report for an evaluation run.

Numbers only. Cost is computed from a rate that is stated explicitly and marked as an
assumption -- the token counts are measured, the price per token is not, and the two
must not be presented with the same confidence.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from compute_metrics import compute, fmt, wilson  # noqa: E402
from eval_assertions import (  # noqa: E402
    check_deterministic_assertions,
    extract_numbers,
    facts_with_numbers,
    numbers_match,
    subset_statistics,
)

# ASSUMPTION, not a measurement. Gemini Flash-Lite class pricing, USD per 1M tokens.
# Override with --input-rate / --output-rate. Token counts are measured exactly.
ASSUMED_INPUT_USD_PER_M = 0.10
ASSUMED_OUTPUT_USD_PER_M = 0.40

CATEGORY_ORDER = [
    "entity_specific", "aggregate", "multi_hop", "unanswerable", "adversarial",
]
CATEGORY_LETTER = {
    "entity_specific": "A", "aggregate": "B", "multi_hop": "C",
    "unanswerable": "D", "adversarial": "E",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", type=Path)
    ap.add_argument("--eval-set", type=Path,
                    default=PROJECT_ROOT / "tests" / "eval" / "eval_set_v1.json")
    ap.add_argument("--input-rate", type=float, default=ASSUMED_INPUT_USD_PER_M)
    ap.add_argument("--output-rate", type=float, default=ASSUMED_OUTPUT_USD_PER_M)
    args = ap.parse_args()

    run = json.loads(args.run.read_text(encoding="utf-8"))
    eval_set = json.loads(args.eval_set.read_text(encoding="utf-8"))
    by_id = {i["id"]: i for i in eval_set["items"]}
    results = run["results"]
    report = compute(run, eval_set)

    # ---------------------------------------------------------------- 1. cost + time
    tot = run["totals"]
    cost_in = tot["input_tokens"] / 1e6 * args.input_rate
    cost_out = tot["output_tokens"] / 1e6 * args.output_rate

    print("=" * 78)
    print("1. COST AND WALL CLOCK")
    print("=" * 78)
    print(f"  wall clock            : {run['wall_clock_ms']/1000:.1f} s "
          f"({run['wall_clock_ms']/60000:.1f} min)")
    lat = [r["latency_ms"] for r in results]
    print(f"  per-item latency      : median {statistics.median(lat):.0f} ms, "
          f"min {min(lat):.0f}, max {max(lat):.0f}")
    print(f"  LLM calls             : {tot['llm_calls']}")
    print(f"  input tokens          : {tot['input_tokens']:,}")
    print(f"  output tokens         : {tot['output_tokens']:,}")
    print(f"  total tokens          : {tot['total_tokens']:,}")
    print(f"  ASSUMED rate          : ${args.input_rate}/1M in, ${args.output_rate}/1M out")
    print(f"  estimated cost        : ${cost_in + cost_out:.4f} "
          f"(in ${cost_in:.4f} + out ${cost_out:.4f})")
    print(f"  cost per item         : ${(cost_in + cost_out)/len(results):.5f}")
    print("  NOTE: token counts are measured; the price per token is an assumption.")

    # ---------------------------------------------------------------- 2. per category
    print()
    print("=" * 78)
    print("2. PER-CATEGORY RESULTS")
    print("=" * 78)

    assertion_rows = report["assertions"]["rows"]
    routing_rows = {r["id"]: r for r in report["routing"]["rows"]}

    for cat in CATEGORY_ORDER:
        cat_results = [r for r in results if by_id[r["id"]]["category"] == cat]

        if not cat_results:
            continue

        ids = {r["id"] for r in cat_results}

        rr = [routing_rows[i] for i in ids if i in routing_rows]
        routing_ci = wilson(sum(1 for x in rr if x["overall"]), len(rr))

        ar = [a for a in assertion_rows
              if a["item_id"] in ids and a["status"] in ("pass", "fail")]
        inconclusive = sum(1 for a in assertion_rows
                           if a["item_id"] in ids and a["status"] == "inconclusive")
        assert_ci = wilson(sum(1 for a in ar if a["status"] == "pass"), len(ar))

        tp = sum(1 for r in cat_results
                 if not by_id[r["id"]]["answerable"] and r["outcome"] == "refused")
        fp = sum(1 for r in cat_results
                 if by_id[r["id"]]["answerable"] and r["outcome"] == "refused")
        fn = sum(1 for r in cat_results
                 if not by_id[r["id"]]["answerable"] and r["outcome"] != "refused")

        scores = [r["evaluator"]["grounding_score"] for r in cat_results
                  if r["evaluator"]["grounding_score"] is not None]

        print(f"\n  [{CATEGORY_LETTER[cat]}] {cat}   items run: {len(cat_results)}")
        print(f"      routing            {fmt(routing_ci)}"
              + ("" if rr else "   (no routed items)"))
        print(f"      assertions         {fmt(assert_ci)}"
              + (f"   +{inconclusive} inconclusive" if inconclusive else ""))

        if tp + fp + fn:
            print(f"      refusal precision  {fmt(wilson(tp, tp + fp))}")
            print(f"      refusal recall     {fmt(wilson(tp, tp + fn))}")
        else:
            print("      refusal            n/a (no refusal-expected items)")

        if scores:
            print(f"      grounding          mean {statistics.fmean(scores):.2f}  "
                  f"min {min(scores)}  n={len(scores)}")
        else:
            print("      grounding          no scored answers")

    print(f"\n  OVERALL routing      {fmt(report['routing']['overall'])}")
    print(f"  OVERALL assertions   {fmt(report['assertions']['overall'])}")
    print(f"  OVERALL refusal precision {fmt(report['refusal']['precision'])}")
    print(f"  OVERALL refusal recall    {fmt(report['refusal']['recall'])}")

    all_scores = [r["evaluator"]["grounding_score"] for r in results
                  if r["evaluator"]["grounding_score"] is not None]
    if all_scores:
        print(f"  OVERALL grounding    mean {statistics.fmean(all_scores):.2f}  "
              f"min {min(all_scores)}  n={len(all_scores)}")

    print("\n  assertion pass rate by type:")
    for atype, block in report["assertions"]["by_type"].items():
        line = f"      {atype:32s} {fmt(block['interval'])}"
        if block["inconclusive"]:
            line += f"   +{block['inconclusive']} inconclusive"
        print(line)
        if block["failures"]:
            print(f"          failures: {block['failures']}")

    # ---------------------------------------------------------------- 3. unexpected
    print()
    print("=" * 78)
    print("3. UNEXPECTED BEHAVIOUR")
    print("=" * 78)

    if report["unexpected_behaviour"]:
        for u in report["unexpected_behaviour"]:
            print(f"  {u['id']}: {u['what']}")
            print(f"      {u['detail']}")
    else:
        print("  none (no answerable question refused, no unanswerable question answered)")

    print("\n  injection scenarios:")
    for qid in ("E74", "E81", "E82", "E83"):
        r = next((x for x in results if x["id"] == qid), None)
        if r is None:
            print(f"    {qid}: NOT RUN (skipped)")
            continue
        ev = r["evidence"]
        touched = ev["sql_records"] + ev["graph_records"] + ev["vector_records"]
        checks = [a for a in check_deterministic_assertions(by_id[qid], r)
                  if a.assertion.startswith("injection")]
        verdict = "FAIL" if any(a.status == "fail" for a in checks) else "pass"
        print(f"    {qid}: outcome={r['outcome']} db_records_touched={touched} "
              f"injection_checks={verdict}")
        for a in checks:
            print(f"        [{a.status}] {a.assertion}: {a.detail[:96]}")

    # ---------------------------------------------------------------- 4. usage gaps
    print()
    print("=" * 78)
    print("4. TOKEN ACCOUNTING GAPS")
    print("=" * 78)
    print(f"  missing_usage_calls   : {tot['missing_usage_calls']}")
    offenders = [r["id"] for r in results if r["llm_usage"]["totals"]["missing_usage"]]
    print(f"  items with None counts: {len(offenders)}  {offenders}")

    # ---------------------------------------------------------------- 5. C43
    print()
    print("=" * 78)
    print("5. C43 -- THE COUNTER-INTUITIVE ITEM")
    print("=" * 78)
    c43 = next((r for r in results if r["id"] == "C43"), None)
    if c43 is None:
        print("  C43 was not run")
    else:
        print(f"  outcome   : {c43['outcome']}   grounding={c43['evaluator']['grounding_score']} "
              f"completeness={c43['evaluator']['completeness_score']} "
              f"readiness={c43['evaluator']['business_readiness_score']}")
        print(f"  route     : sql={c43['actual']['sql_intent']} "
              f"graph={c43['actual']['graph_intent']} vec={c43['actual']['vector_artifact_groups']}")
        print(f"  evidence  : sql={c43['evidence']['sql_records']} "
              f"graph={c43['evidence']['graph_records']} vector={c43['evidence']['vector_records']}")
        print(f"  unsupported claims: {c43['evaluator']['unsupported_claims']}")
        answer = c43["answer_text"] or ""
        low = answer.lower()
        signals = {
            "states rho 0.14": any(numbers_match(n, 0.14) for n in extract_numbers(answer)),
            "states rho 0.393/0.376": any(
                numbers_match(n, v) for n in extract_numbers(answer) for v in (0.393, 0.376)
            ),
            "mentions late delivery rate": "late" in low and "rate" in low,
            "asserts NO strong scaling": any(
                p in low for p in ("does not scale", "no strong", "not strongly",
                                   "weak", "does not track", "not proportional",
                                   "no clear relationship", "little relationship")
            ),
            "asserts scaling (WRONG)": any(
                p in low for p in ("scales with", "increases with order volume",
                                   "proportional to order volume", "more orders means more complaints")
            ),
        }
        print("  answer signal check:")
        for k, v in signals.items():
            print(f"      {k:34s} {v}")
        print("\n  --- full answer ---")
        print(answer if answer else "(no answer text)")
        print("  --- end answer ---")

    # ---------------------------------------------------------------- 6. limit gaps
    print()
    print("=" * 78)
    print("6. LIMIT-AWARENESS: LARGEST SUBSET-VS-POPULATION GAPS")
    print("=" * 78)

    rows = []
    for r in results:
        item = by_id[r["id"]]
        if "limit_awareness" not in item["flags"]:
            continue

        pop = [n for _, nums in facts_with_numbers(item) for n in nums]
        ev = r.get("evidence") or {}
        recs = list(ev.get("sql_record_sample") or []) + list(ev.get("graph_record_sample") or [])
        stats = subset_statistics(recs)
        answer_numbers = extract_numbers(r.get("answer_text") or "")

        best = None
        for name, sv in stats.items():
            if abs(sv) <= 2:
                continue
            for p in pop:
                if p == 0 or numbers_match(sv, p):
                    continue
                ratio = max(abs(sv), abs(p)) / max(min(abs(sv), abs(p)), 1e-9)
                if best is None or ratio > best["ratio"]:
                    best = {"stat": name, "subset": sv, "population": p, "ratio": ratio}

        if best is None:
            continue

        best["id"] = r["id"]
        best["answer_states_subset"] = any(
            numbers_match(n, best["subset"]) for n in answer_numbers
        )
        best["answer_states_population"] = any(
            numbers_match(n, best["population"]) for n in answer_numbers
        )
        rows.append(best)

    rows.sort(key=lambda x: -x["ratio"])
    print(f"  {'id':6s} {'statistic':28s} {'subset':>14s} {'population':>14s} {'ratio':>8s} "
          f"{'says subset':>12s} {'says pop':>9s}")
    for row in rows[:3]:
        print(f"  {row['id']:6s} {row['stat'][:28]:28s} {row['subset']:14.4g} "
              f"{row['population']:14.4g} {row['ratio']:8.1f}x "
              f"{str(row['answer_states_subset']):>12s} {str(row['answer_states_population']):>9s}")

    print(f"\n  wrong-denominator count: "
          f"{sum(1 for r in rows if r['answer_states_subset'])} of {len(rows)} "
          "limit_awareness items state a subset-only figure")


if __name__ == "__main__":
    main()
