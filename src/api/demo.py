"""Demo mode: replay recorded runs, call nothing (M8 Task 3).

`DEMO_MODE=true` makes the API serve responses recorded from real runs against the real
corpus, so a visitor needs no API key, the project spends no budget, and no database has
to be reachable.

Zero LLM calls and zero database queries is a **structural** property here, not a
promise. When DEMO_MODE is set, `src/api/main.py` never imports `agentic_workflow` or
`hybrid_retriever`, so torch, SQLAlchemy, psycopg2, the Neo4j and Qdrant drivers and
google-genai are never loaded -- and `requirements-demo.txt` does not install them at
all. A demo container physically cannot reach a model or a database.
`tests/test_demo_mode.py` asserts the absent imports; the demo image asserts the rest by
not containing the packages.

This module imports nothing beyond the standard library, by design.
"""

from __future__ import annotations

import json
import os
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_PATH = Path(os.getenv("DEMO_BUNDLE", PROJECT_ROOT / "demo" / "scenarios.json"))

# Below this, a question is not a paraphrase of any recorded one. Returning the closest
# recording for an unrelated question would be the demo lying about what it holds.
MATCH_THRESHOLD = 0.62

_LOCK = threading.Lock()
_BUNDLE: dict[str, Any] | None = None


class DemoBundleMissing(RuntimeError):
    """DEMO_MODE is on and there is nothing to replay. Fail loudly at startup."""


def demo_mode_enabled() -> bool:
    return os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def load_bundle(force: bool = False) -> dict[str, Any]:
    global _BUNDLE

    with _LOCK:
        if _BUNDLE is not None and not force:
            return _BUNDLE

        if not BUNDLE_PATH.exists():
            raise DemoBundleMissing(
                f"DEMO_MODE is enabled but {BUNDLE_PATH} does not exist. "
                f"Build it with: python scripts/build_demo_bundle.py"
            )

        bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))

        if not bundle.get("scenarios"):
            raise DemoBundleMissing(f"{BUNDLE_PATH} contains no scenarios")

        _BUNDLE = bundle

        return _BUNDLE


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def scenario_summaries() -> list[dict[str, Any]]:
    """What GET /demo/scenarios returns: questions and route summaries."""
    bundle = load_bundle()

    return [
        {
            "id": scenario["id"],
            "question": scenario["question"],
            "category": scenario["category"],
            "headline": scenario["headline"],
            "shows": scenario["shows"],
            "caveat": scenario.get("caveat"),
            "run_id": scenario["response"]["run_id"],
            "outcome": scenario["response"]["overall_status"],
            "answerable": scenario["response"]["answerable"],
            "route": scenario["route"],
            "evidence": scenario["evidence"],
            "metrics": scenario["metrics"],
            "grounding_score": (scenario.get("evaluator") or {}).get("grounding_score"),
            "route_reproduces_today": scenario.get("route_reproduces_today", {}),
        }
        for scenario in bundle["scenarios"]
    ]


def find_scenario(question: str) -> tuple[dict[str, Any] | None, float]:
    """Exact match first, then closest paraphrase above the threshold."""
    bundle = load_bundle()
    target = normalise(question)

    for scenario in bundle["scenarios"]:
        if normalise(scenario["question"]) == target:
            return scenario, 1.0

    best: dict[str, Any] | None = None
    best_score = 0.0

    for scenario in bundle["scenarios"]:
        score = SequenceMatcher(None, target, normalise(scenario["question"])).ratio()

        if score > best_score:
            best, best_score = scenario, score

    if best_score >= MATCH_THRESHOLD:
        return best, best_score

    return None, best_score


def unmatched_response(question: str, best_score: float) -> dict[str, Any]:
    """The same field set as any other response -- a distinct status, not a fake answer.

    Reusing `REFUSED` here would conflate "the system declined this question" with
    "this deployment holds no recording of it", which are different facts about
    different things.
    """
    bundle = load_bundle()
    available = ", ".join(f'"{s["question"]}"' for s in bundle["scenarios"][:3])

    reason = (
        f"Demo mode holds {len(bundle['scenarios'])} recorded runs and none of them "
        f"matches this question (closest match {best_score:.2f}, threshold "
        f"{MATCH_THRESHOLD}). This is a replay of recorded output, not a live system: "
        f"no model was called. Try one of the recorded questions, for example "
        f"{available}. GET /demo/scenarios lists them all."
    )

    return {
        "overall_status": "DEMO_NO_SCENARIO",
        "run_id": "demo_no_scenario",
        "query": question,
        "answerable": False,
        "refusal_reason": reason,
        "answer_provider": "none",
        "answer_model": "none",
        "answer_length_chars": len(reason),
        "source_summary": {},
        "evaluation_summary": {},
        "output_files": {},
        "answer_preview": reason,
    }


def replay(question: str) -> dict[str, Any]:
    """The recorded workflow result for a question, shaped exactly like a live one."""
    scenario, score = find_scenario(question)

    if scenario is None:
        return unmatched_response(question, score)

    # A copy: the bundle is loaded once per process and must not be mutated by a request.
    return dict(scenario["response"])


def trace_for(run_id: str) -> list[dict[str, Any]] | None:
    bundle = load_bundle()

    for scenario in bundle["scenarios"]:
        if scenario["response"]["run_id"] == run_id:
            return [dict(span) for span in scenario["trace"]["spans"]]

    return None


def bundle_info() -> dict[str, Any]:
    bundle = load_bundle()

    return {
        "schema_version": bundle["schema_version"],
        "source_run": bundle["source_run"],
        "source_run_generated_at": bundle["source_run_generated_at"],
        "scenario_count": bundle["scenario_count"],
        "note": bundle["note"],
    }
