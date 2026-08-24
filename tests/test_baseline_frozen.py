"""M0 task 5: the frozen baseline exists, is coherent, and still describes this repo.

tests/baseline/baseline_results.json is the before-state every later milestone is
measured against. These tests do not re-run the workflow -- they check that the file
is present, complete, and still consistent with the source it was captured from, so
that a milestone cannot quietly drift away from its own reference point.

Also frozen here: the circularity findings from AUDIT.md P3. The five validation
cases are the planner's own few-shot examples, and the same five cases appear in
three separate validators. Those are asserted as facts about the current source, so
that fixing them is a visible edit.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "tests" / "baseline" / "baseline_results.json"
PLANNER = PROJECT_ROOT / "src" / "planning" / "gemini_query_planner.py"

VALIDATORS_WITH_CASES = [
    "src/orchestration/agentic_workflow_validator.py",
    "src/api/api_validator.py",
    "src/retrieval/retrieval_context_validator.py",
]


@pytest.fixture(scope="module")
def baseline() -> dict:
    if not BASELINE.exists():
        pytest.fail(f"{BASELINE} is missing. It must never be deleted.")
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _test_case_queries(relative_path: str) -> list[str]:
    """Pull the literal `query` values out of a validator's TEST_CASES list."""
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "TEST_CASES"
            and isinstance(node.value, ast.List)
        ):
            queries = []
            for element in node.value.elts:
                if isinstance(element, ast.Dict):
                    for key, value in zip(element.keys, element.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "query"
                            and isinstance(value, ast.Constant)
                        ):
                            queries.append(value.value)
            return queries
    raise AssertionError(f"TEST_CASES not found in {relative_path}")


# --- the file itself ----------------------------------------------------------


def test_baseline_is_committed():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert "tests/baseline/baseline_results.json" in tracked


def test_baseline_shape(baseline):
    assert baseline["schema_version"] == 1
    assert baseline["milestone"] == "M0"
    assert baseline["environment"]["git_commit"]
    assert len(baseline["questions"]) == 10


def test_baseline_covers_the_three_required_groups(baseline):
    groups: dict[str, int] = {}
    for question in baseline["questions"]:
        groups[question["group"]] = groups.get(question["group"], 0) + 1
    assert groups == {
        "five_validation_cases": 5,
        "document_paraphrases": 3,
        "controls": 2,
    }


def test_every_question_records_an_outcome(baseline):
    for question in baseline["questions"]:
        assert question["outcome"] in {"completed", "crashed"}, question["id"]
        if question["outcome"] == "completed":
            assert question["route"]["sql_intent"]
            assert question["evidence"]["sql"]["records_sha256"]
            assert question["answer"]["sha256"]
        else:
            assert question["exception_type"]
            assert question["exception_message"]


def test_controls_are_present(baseline):
    ids = {question["id"] for question in baseline["questions"]}
    assert "control_purple_monkey_dishwasher" in ids
    assert "control_highest_revenue_seller" in ids


# --- what the baseline says about the system ---------------------------------


def test_sql_only_plans_still_crash(baseline):
    """AUDIT.md P3 / REBUILD_PLAN M2.

    'Which seller had the highest revenue?' is the project's own worked example and
    it cannot complete, because validate_and_normalize_plan requires a non-null graph
    intent (gemini_query_planner.py:201).
    """
    question = next(
        q for q in baseline["questions"] if q["id"] == "control_highest_revenue_seller"
    )
    assert question["outcome"] == "crashed"
    assert question["exception_type"] == "ValueError"
    assert "graph intent" in question["exception_message"]


def test_different_questions_still_return_identical_evidence(baseline):
    """AUDIT.md F-03, expressed as a number.

    No template binds anything but :limit, so the evidence answers the intent rather
    than the question. In the baseline capture, four different questions -- including
    two of the document's own paraphrases -- came back with byte-identical SQL
    evidence. M2's exit criterion is that identical_evidence_groups becomes empty.
    """
    groups = baseline["cross_question"]["identical_evidence_groups"]
    assert groups, (
        "no two questions share evidence any more -- F-03 may be fixed; "
        "if so, delete this test and record the change"
    )
    collided = {qid for ids in groups.values() for qid in ids}
    assert len(collided) >= 4, collided


def test_review_intelligence_evidence_is_not_reproducible(baseline):
    """Found during M0 task 5; not in the original audit.

    The two questions that routed to review_intelligence got different rows. The
    template orders by review_score alone, 11,424 rows tie at score 1, and LIMIT 10
    takes an arbitrary ten -- so the same question asked twice can be answered from
    different evidence. scripts/check_template_determinism.py measures it directly:
    1 unstable template out of 11.
    """
    per_intent = baseline["cross_question"]["sql_evidence_sets_per_intent"]
    assert per_intent.get("review_intelligence") == 2, per_intent
    stable_intents = {k: v for k, v in per_intent.items() if k != "review_intelligence"}
    assert all(count == 1 for count in stable_intents.values()), stable_intents


# --- the circularity, frozen as facts about the source -----------------------


def test_the_five_cases_are_the_planners_own_few_shot_examples():
    """AUDIT.md P3: the planner is graded on its own answer sheet."""
    prompt_source = PLANNER.read_text(encoding="utf-8")
    queries = _test_case_queries("src/orchestration/agentic_workflow_validator.py")
    assert len(queries) == 5
    embedded = [q for q in queries if q in prompt_source]
    assert embedded == queries, (
        "not all five validation cases appear verbatim in the planner prompt; "
        f"missing: {[q for q in queries if q not in embedded]}"
    )


def test_three_validators_share_one_identical_case_list():
    """'5 cases passed' three times is one result reported three times."""
    lists = {path: _test_case_queries(path) for path in VALIDATORS_WITH_CASES}
    reference = lists[VALIDATORS_WITH_CASES[0]]
    for path, queries in lists.items():
        assert queries == reference, f"{path} diverged from {VALIDATORS_WITH_CASES[0]}"


def test_baseline_queries_match_the_validator_cases():
    """The baseline must be capturing the cases the project actually claims."""
    baseline_data = json.loads(BASELINE.read_text(encoding="utf-8"))
    captured = [
        question["query"]
        for question in baseline_data["questions"]
        if question["group"] == "five_validation_cases"
    ]
    assert captured == _test_case_queries("src/orchestration/agentic_workflow_validator.py")
