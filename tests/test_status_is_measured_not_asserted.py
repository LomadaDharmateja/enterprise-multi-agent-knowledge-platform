"""A reported status must be measured, never asserted (AUDIT.md F-18).

Found by the M9 re-audit, still open after seven milestones.

    `synthetic_data_generator.py:892` and `hybrid_retriever.py:552` both write
    `"overall_status": "PASS"` as a literal into their JSON reports. ... Related:
    `agentic_workflow.py:265` defaults the workflow's top-level status to `"PASS"`
    when the evaluation summary is missing -- the pipeline fails open.

Both halves were still live, and M4 made the first one worse: once graceful
degradation existed, a retrieval with PostgreSQL down produced a report that said
`PASS` in one field and named a dead dependency three fields later.

This is the same defect M7 fixed in the tracer -- a status initialised to success and
downgraded only if something remembers to -- so it gets the same treatment: the
success value has to be *derived*, and a test has to fail if it goes back to being
typed in.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETRIEVER = PROJECT_ROOT / "src" / "retrieval" / "hybrid_retriever.py"
WORKFLOW = PROJECT_ROOT / "src" / "orchestration" / "agentic_workflow.py"


def code_only(path: Path) -> str:
    """Source with comments stripped.

    A grep for a literal also matches the comment explaining why the literal was
    removed -- which is how the first version of this test failed on the very fix it
    was written to protect. The check is about code, so it reads code.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    for token in tokenize.generate_tokens(io.StringIO("".join(lines)).readline):
        if token.type != tokenize.COMMENT:
            continue

        row = token.start[0] - 1
        start, end = token.start[1], token.end[1]
        # Blank the comment in place. Replacing it with spaces rather than removing it
        # keeps every other line and column exactly where it was, so the assertions
        # below still match real code spans.
        lines[row] = lines[row][:start] + " " * (end - start) + lines[row][end:]

    return "".join(lines)


def build_report(unavailable: list[str]) -> dict:
    """Reproduce the report summary the retriever builds, for given dead legs.

    The retriever's report block is not extractable without running a full retrieval,
    so this asserts the rule it now follows. `test_the_retriever_derives_its_status`
    below checks that the source really does derive it.
    """
    legs = [{"source": name, "unavailable": True} for name in unavailable]

    return {"overall_status": "DEGRADED" if legs else "PASS", "unavailable_legs": legs}


def test_a_report_naming_a_dead_dependency_cannot_say_pass():
    degraded = build_report(["postgresql"])

    assert degraded["overall_status"] == "DEGRADED"
    assert degraded["unavailable_legs"]

    healthy = build_report([])

    assert healthy["overall_status"] == "PASS"
    assert not healthy["unavailable_legs"]


def test_the_retriever_derives_its_status_rather_than_typing_it():
    """The literal `"overall_status": "PASS"` must not reappear in the report block."""
    source = code_only(RETRIEVER)

    # The summary dict is the one that also carries `unavailable_legs`.
    block = source[source.index('"unavailable_legs"') - 2000:
                   source.index('"unavailable_legs"') + 200]

    assert '"overall_status": "PASS"' not in block, (
        "the retrieval report is asserting PASS again instead of deriving it"
    )
    assert '"DEGRADED" if unavailable_legs else "PASS"' in source


def test_the_workflow_does_not_fail_open_when_there_is_no_verdict():
    """An absent evaluator verdict must not read as success."""
    source = code_only(WORKFLOW)

    assert 'evaluation_summary.get("overall_status", "PASS")' not in source, (
        "the workflow defaults its top-level status to PASS again -- a run whose "
        "evaluator produced no verdict would report success"
    )
    assert 'evaluation_summary.get("overall_status") or "UNKNOWN"' in source


@pytest.mark.parametrize(
    "summary,expected",
    [
        ({"overall_status": "PASS"}, "PASS"),
        ({"overall_status": "FAIL"}, "FAIL"),
        ({}, "UNKNOWN"),
        ({"overall_status": None}, "UNKNOWN"),
        ({"overall_status": ""}, "UNKNOWN"),
    ],
)
def test_the_missing_verdict_resolves_to_unknown(summary, expected):
    """The rule itself, including the empty-string case `.get(k, default)` misses."""
    assert (summary.get("overall_status") or "UNKNOWN") == expected


def test_no_module_in_the_runtime_path_types_a_passing_status_into_a_report():
    """Scoped to the query path.

    The loaders and validators legitimately set PASS after their own checks run --
    that is a computed verdict with the computation right above it. The runtime
    retrieval and orchestration path is where a typed-in PASS is a lie about work
    that may not have happened.
    """
    offenders = []

    for path in (RETRIEVER, WORKFLOW):
        text = code_only(path)

        for match in re.finditer(r'"overall_status":\s*"PASS"', text):
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line}")

    assert not offenders, f"a literal PASS status in the runtime path: {offenders}"
