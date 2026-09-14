"""Re-run the claim audit against the finished system (M9).

The M9 exit criterion is "zero contradicted claims, machine-checked". This is the
machine. It reads audit/claims.json, runs each claim's check, prints the before/after
verdict table, writes audit/results.json, and exits non-zero if any claim is
CONTRADICTED.

    python scripts/audit_claims.py                 # everything
    python scripts/audit_claims.py --only 29,30,34 # a subset
    python scripts/audit_claims.py --skip-slow     # no full-suite or pytest checks

What it deliberately does NOT do is let a claim pass by being deleted. Three verdicts
exist precisely so that cannot happen quietly:

    SUPERSEDED    was true at the first audit, false now because the gap was closed
    NOT_CLAIMED   the current documentation does not assert it -- with a reason
    UNVERIFIABLE  no command settles it

Each is counted and listed separately in the summary. "Zero contradicted" means
nothing when thirty claims quietly became NOT_CLAIMED, so the report shows both.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit import checks as C  # noqa: E402

MANIFEST = PROJECT_ROOT / "audit" / "claims.json"
RESULTS = PROJECT_ROOT / "audit" / "results.json"

SLOW = {"pytest_passes", "test_suite_passes"}

ORDER = [C.CONTRADICTED, C.SUPERSEDED, C.CAVEAT, C.CONFIRMED, C.NOT_CLAIMED,
         C.UNVERIFIABLE, "ERROR"]


def run_claim(claim: dict, skip_slow: bool) -> dict:
    name = claim["check"]
    kwargs = {k: v for k, v in claim.items()
              if k not in {"id", "claim", "baseline_verdict", "baseline_evidence",
                           "check", "inherited", "note"}}

    if skip_slow and name in SLOW:
        return {"verdict": "SKIPPED", "evidence": "slow check skipped (--skip-slow)"}

    fn = C.REGISTRY.get(name)

    if fn is None:
        return {"verdict": "ERROR", "evidence": f"no check registered named {name!r}"}

    started = time.perf_counter()

    try:
        verdict, evidence = fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        # An exploding check is not a passing claim. It is reported as ERROR and the
        # run fails, because "the check crashed" must never read as "the claim holds".
        return {
            "verdict": "ERROR",
            "evidence": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=3),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    return {
        "verdict": verdict,
        "evidence": evidence,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="comma-separated claim ids")
    ap.add_argument("--skip-slow", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    claims = manifest["claims"]

    if args.only:
        wanted = {int(i) for i in args.only.split(",")}
        claims = [c for c in claims if c["id"] in wanted]

    results = []

    for claim in claims:
        outcome = run_claim(claim, args.skip_slow)
        row = {**claim, **outcome}
        results.append(row)

        if not args.quiet:
            changed = "" if row["verdict"] == claim["baseline_verdict"] else "  <-- changed"
            print(f"{row['id']:>3}  {claim['baseline_verdict']:<12} -> {row['verdict']:<12}"
                  f"{changed}")
            print(f"     {claim['claim'][:96]}")
            print(f"     {str(row['evidence'])[:150]}")

    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in ORDER}
    counts = {k: v for k, v in counts.items() if v}
    counts.update({v: sum(1 for r in results if r["verdict"] == v)
                   for v in ("SKIPPED",) if any(r["verdict"] == "SKIPPED" for r in results)})

    print("\n" + "=" * 78)
    print(f"CLAIM AUDIT  --  {len(results)} claims")
    print("=" * 78)

    for verdict in ORDER + ["SKIPPED"]:
        n = sum(1 for r in results if r["verdict"] == verdict)
        if n:
            print(f"  {verdict:<14} {n}")

    contradicted = [r for r in results if r["verdict"] == C.CONTRADICTED]
    errors = [r for r in results if r["verdict"] == "ERROR"]
    fixed = [r for r in results
             if r["baseline_verdict"] == C.CONTRADICTED and r["verdict"] != C.CONTRADICTED]

    print(f"\n  baseline contradicted : "
          f"{sum(1 for r in results if r['baseline_verdict'] == C.CONTRADICTED)}")
    print(f"  still contradicted    : {len(contradicted)}")
    print(f"  no longer contradicted: {len(fixed)}  {[r['id'] for r in fixed]}")

    superseded = [r for r in results if r["verdict"] == C.SUPERSEDED]
    not_claimed = [r for r in results if r["verdict"] == C.NOT_CLAIMED]

    if superseded:
        print(f"\n  SUPERSEDED ({len(superseded)}) -- true then, false now because the "
              f"gap was closed.\n  The README must not repeat these:")
        for r in superseded:
            print(f"    #{r['id']:<4} {r['claim'][:82]}")

    if not_claimed:
        print(f"\n  NOT_CLAIMED ({len(not_claimed)}) -- dropped rather than fixed:")
        for r in not_claimed:
            print(f"    #{r['id']:<4} {r['claim'][:82]}")

    if contradicted:
        print(f"\n  CONTRADICTED ({len(contradicted)}):")
        for r in contradicted:
            print(f"    #{r['id']:<4} {r['claim'][:82]}")
            print(f"           {str(r['evidence'])[:130]}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}) -- a check that crashed is not a claim that holds:")
        for r in errors:
            print(f"    #{r['id']:<4} {str(r['evidence'])[:110]}")

    RESULTS.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseline_audit": manifest["baseline_audit"],
        "coverage": manifest["coverage"],
        "counts": {v: sum(1 for r in results if r["verdict"] == v)
                   for v in ORDER + ["SKIPPED"]
                   if sum(1 for r in results if r["verdict"] == v)},
        "still_contradicted": [r["id"] for r in contradicted],
        "errors": [r["id"] for r in errors],
        "results": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nreport: {RESULTS}")

    sys.exit(1 if (contradicted or errors) else 0)


if __name__ == "__main__":
    main()
