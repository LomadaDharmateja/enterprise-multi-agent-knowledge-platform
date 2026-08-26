"""M5 Tasks 3 and 4: cache replay measurement and concurrency test.

Replay: run N eval-set questions cold, then replay the identical questions against the
warm cache and report hit rate, token savings and latency reduction.

Concurrency: run 5 different questions (different routes) simultaneously and report
throughput, errors, and whether the embedding-model cache and circuit breakers behaved.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

for _d in (
    PROJECT_ROOT / "src" / "orchestration",
    PROJECT_ROOT / "src" / "retrieval",
    PROJECT_ROOT / "src" / "observability",
):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from agentic_workflow import run_agentic_workflow  # noqa: E402
from llm_usage import collect  # noqa: E402
from query_cache import get_cache, reset_cache  # noqa: E402

EVAL_SET = PROJECT_ROOT / "tests" / "eval" / "eval_set_v1.json"

IN_RATE = 0.25 / 1e6
OUT_RATE = 1.50 / 1e6


def load_questions(limit: int, ids: list[str] | None = None):
    items = json.loads(EVAL_SET.read_text(encoding="utf-8"))["items"]
    items = [i for i in items if "requires_fixture" not in i["flags"]]

    if ids:
        wanted = {i.lower() for i in ids}
        items = [i for i in items if i["id"].lower() in wanted]

    return items[:limit] if limit else items


def run_one(item, output_root: Path):
    started = time.perf_counter()

    with collect() as calls:
        response = run_agentic_workflow(
            query=item["question"], output_dir=output_root / item["id"]
        )

    return {
        "id": item["id"],
        "outcome": "refused" if response.get("answerable") is False else "completed",
        "cache_hit": bool((response.get("cache") or {}).get("hit")),
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "calls": len(calls),
        "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
        "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
    }


def summarise(rows, label):
    lat = [r["latency_ms"] for r in rows]
    tin = sum(r["input_tokens"] for r in rows)
    tout = sum(r["output_tokens"] for r in rows)

    return {
        "label": label,
        "items": len(rows),
        "hits": sum(1 for r in rows if r["cache_hit"]),
        "llm_calls": sum(r["calls"] for r in rows),
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": round(tin * IN_RATE + tout * OUT_RATE, 6),
        "latency_total_ms": round(sum(lat), 1),
        "latency_mean_ms": round(statistics.fmean(lat), 1),
        "latency_median_ms": round(statistics.median(lat), 1),
    }


def cache_replay(limit: int, output_root: Path):
    items = load_questions(limit)

    reset_cache()
    cache = get_cache()

    print(f"=== CACHE REPLAY: {len(items)} questions ===\n")
    print("--- pass 1 (cold) ---")
    cold = []

    for index, item in enumerate(items, 1):
        row = run_one(item, output_root / "cold")
        cold.append(row)
        print(f"  [{index}/{len(items)}] {row['id']:5s} {row['outcome']:9s} "
              f"hit={row['cache_hit']!s:5s} {row['latency_ms']:8.0f}ms "
              f"calls={row['calls']}")

    print("\n--- pass 2 (warm, identical questions) ---")
    warm = []

    for index, item in enumerate(items, 1):
        row = run_one(item, output_root / "warm")
        warm.append(row)
        print(f"  [{index}/{len(items)}] {row['id']:5s} {row['outcome']:9s} "
              f"hit={row['cache_hit']!s:5s} {row['latency_ms']:8.0f}ms "
              f"calls={row['calls']}")

    a, b = summarise(cold, "cold"), summarise(warm, "warm")

    refused_cold = {r["id"] for r in cold if r["outcome"] == "refused"}
    refused_hits = sum(
        1 for r in warm if r["id"] in refused_cold and r["cache_hit"]
    )

    print("\n" + "=" * 68)
    print(f"{'':22s} {'cold':>14s} {'warm':>14s} {'reduction':>14s}")
    for key, fmt in (
        ("hits", "{:.0f}"), ("llm_calls", "{:.0f}"),
        ("input_tokens", "{:,.0f}"), ("output_tokens", "{:,.0f}"),
        ("cost_usd", "${:.5f}"), ("latency_total_ms", "{:,.0f}"),
        ("latency_mean_ms", "{:,.0f}"),
    ):
        red = ""
        if a[key]:
            red = f"{(a[key]-b[key])/a[key]*100:.1f}%"
        print(f"{key:22s} {fmt.format(a[key]):>14s} {fmt.format(b[key]):>14s} {red:>14s}")

    print(f"\n  warm hit rate            : {b['hits']}/{b['items']} = "
          f"{b['hits']/b['items']*100:.1f}%")
    print(f"  refused-on-cold items    : {len(refused_cold)}")
    print(f"  of those, hit on replay  : {refused_hits}/{len(refused_cold)}")
    print(f"  cache stats endpoint     : "
          f"{json.dumps(cache.as_dict({'input': 9515.0, 'output': 1067.0}))}")

    return {"cold": a, "warm": b, "refused_cold": sorted(refused_cold),
            "refused_hits": refused_hits, "rows": {"cold": cold, "warm": warm}}


def concurrency(ids: list[str], output_root: Path, workers: int = 5):
    items = load_questions(0, ids)

    print(f"\n=== CONCURRENCY: {len(items)} questions, {workers} workers ===\n")
    for i in items:
        print(f"  {i['id']}: {i['question'][:64]}")

    reset_cache()

    import hybrid_retriever
    from resilience import breaker_states, reset_all_breakers

    reset_all_breakers()
    hybrid_retriever._EMBEDDING_MODELS.clear()

    built = {"n": 0}
    real = hybrid_retriever.SentenceTransformer

    def counting(*args, **kwargs):
        built["n"] += 1
        return real(*args, **kwargs)

    hybrid_retriever.SentenceTransformer = counting

    started = time.perf_counter()
    rows, errors = [], []

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_one, item, output_root / "concurrent"): item
                for item in items
            }

            for future in as_completed(futures):
                item = futures[future]

                try:
                    rows.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append({"id": item["id"], "error": f"{type(exc).__name__}: {exc}"})
    finally:
        hybrid_retriever.SentenceTransformer = real

    wall = time.perf_counter() - started

    print(f"\n  completed        : {len(rows)}/{len(items)}")
    print(f"  errors           : {len(errors)} {errors if errors else ''}")
    print(f"  wall clock       : {wall:.1f}s")
    print(f"  throughput       : {len(rows)/wall:.3f} queries/second")

    if rows:
        lat = [r["latency_ms"] for r in rows]
        print(f"  per-query latency: mean {statistics.fmean(lat):.0f}ms "
              f"median {statistics.median(lat):.0f}ms max {max(lat):.0f}ms")
        serial = sum(lat) / 1000
        print(f"  serial equivalent: {serial:.1f}s -> speedup {serial/wall:.2f}x")

    print(f"\n  embedding model constructions under {workers} concurrent first-uses: "
          f"{built['n']} (must be 1)")
    print(f"  circuit breakers : {json.dumps(breaker_states())}")

    return {
        "items": len(items), "completed": len(rows), "errors": errors,
        "wall_seconds": round(wall, 2),
        "throughput_qps": round(len(rows) / wall, 4) if wall else None,
        "model_constructions": built["n"],
        "breakers": breaker_states(), "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["replay", "concurrency", "both"], default="both")
    ap.add_argument("--replay-limit", type=int, default=12)
    ap.add_argument("--concurrent-ids", nargs="*",
                    default=["A01", "B21", "C33", "E72", "D49"])
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--output-root", type=Path,
                    default=PROJECT_ROOT / "reports" / "m5")
    ap.add_argument("--report", type=Path,
                    default=PROJECT_ROOT / "tests" / "eval" / "results" / "m5_cache_concurrency.json")
    args = ap.parse_args()

    result = {}

    if args.mode in ("replay", "both"):
        result["replay"] = cache_replay(args.replay_limit, args.output_root)

    if args.mode in ("concurrency", "both"):
        result["concurrency"] = concurrency(args.concurrent_ids, args.output_root, args.workers)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nreport: {args.report}")


if __name__ == "__main__":
    main()
