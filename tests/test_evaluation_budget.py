"""The evaluator must never silently drop an artifact group to fit its char budget.

Found in M2 Task 4. `compact_json(document_evidence, max_chars=9000)` chopped the tail
off a 12,597-character payload, which removed the entire `policy_documents` group. The
evaluator then reported the answer's policy IDs as unsupported claims -- correct given
what it was shown, and wrong about the system. Grounding scored 2 and the run FAILed.

With the groups preserved the same answer scored 5 and passed. That is a two-point
swing caused by prompt assembly, not by answer quality, which is exactly the kind of
thing an evaluation number must not be silently sensitive to.
"""

from __future__ import annotations

import json
import re

from gemini_answer_evaluator import compact_json, fit_document_evidence


def make_document_evidence(records_per_group: int = 5, preview_chars: int = 350) -> dict:
    groups = {}

    for group, prefix in (
        ("support_tickets", "TCK"),
        ("warranty_claims", "WRN"),
        ("policy_documents", "POL"),
    ):
        groups[group] = [
            {
                "score": 0.5,
                "artifact_group": group,
                "artifact_type": group,
                "artifact_id": f"{group}:{prefix}-{index:06d}",
                "title": f"{group} record {index}",
                "text_preview": "x" * preview_chars,
                **({"policy_id": f"{prefix}-{index:06d}"} if prefix == "POL" else {}),
            }
            for index in range(1, records_per_group + 1)
        ]

    return {
        "source": "qdrant",
        "retrieval_role": "semantic_document_retrieval",
        "artifact_groups": list(groups),
        "total_records_returned": sum(len(v) for v in groups.values()),
        "results_by_artifact_group": groups,
    }


def test_the_old_tail_chop_produces_broken_partial_evidence():
    """Characterises the defect, so the fix has something to be measured against.

    The chop is size-dependent -- which records fall off depends on how long the
    previews happen to be -- so this asserts the two properties that hold whatever
    the sizes are: the payload is no longer valid JSON, and the last group is
    incomplete. On the real flagship context it removed all five policy records.
    """
    evidence = make_document_evidence()

    chopped = compact_json(evidence, max_chars=9000)

    assert "...TRUNCATED..." in chopped

    try:
        json.loads(chopped)
    except json.JSONDecodeError:
        pass
    else:  # pragma: no cover - would mean the chop stopped truncating
        raise AssertionError("expected the tail chop to produce invalid JSON")

    expected = len(evidence["results_by_artifact_group"]["policy_documents"])
    survived = len(set(re.findall(r"POL-\d+", chopped)))

    assert survived < expected, (
        f"the last group kept all {expected} records; this test needs a payload "
        "that actually overflows the budget"
    )


def test_fitting_keeps_every_artifact_group():
    evidence = make_document_evidence()

    fitted, notes = fit_document_evidence(evidence, max_chars=9000)

    assert set(fitted["results_by_artifact_group"]) == {
        "support_tickets",
        "warranty_claims",
        "policy_documents",
    }

    for group, records in fitted["results_by_artifact_group"].items():
        assert records, f"{group} was emptied"

    assert notes, "trimming happened but was not reported"


def test_fitting_keeps_the_policy_identifiers():
    fitted, _ = fit_document_evidence(make_document_evidence(), max_chars=9000)

    rendered = json.dumps(fitted, default=str)

    assert re.findall(r"POL-\d+", rendered), "policy IDs did not survive the budget"


def test_fitting_respects_the_budget():
    fitted, _ = fit_document_evidence(make_document_evidence(), max_chars=9000)

    rendered = json.dumps(fitted, indent=2, ensure_ascii=False, default=str)

    assert len(rendered) <= 9000, len(rendered)


def test_small_evidence_is_left_completely_alone():
    evidence = make_document_evidence(records_per_group=1, preview_chars=50)

    fitted, notes = fit_document_evidence(evidence, max_chars=9000)

    assert notes == []
    assert fitted == evidence
    assert "trimmed_for_evaluation" not in fitted


def test_trimming_is_reported_not_silent():
    """Silent loss is the failure mode; a stated trim is acceptable."""
    fitted, notes = fit_document_evidence(make_document_evidence(), max_chars=9000)

    assert fitted["trimmed_for_evaluation"] == notes
    assert any("shortened" in note or "limited" in note for note in notes)


def test_every_group_survives_even_an_absurdly_small_budget():
    fitted, notes = fit_document_evidence(make_document_evidence(), max_chars=200)

    assert set(fitted["results_by_artifact_group"]) == {
        "support_tickets",
        "warranty_claims",
        "policy_documents",
    }
    assert all(records for records in fitted["results_by_artifact_group"].values())
    assert any("exceeds the evaluation budget" in note for note in notes)


def test_the_evaluation_prompt_contains_the_policy_ids():
    """End of the chain: the IDs must be in the string the evaluator actually reads."""
    from gemini_answer_evaluator import build_evaluation_prompt

    context = {
        "business_question": "which sellers have complaints",
        "source_summary": {},
        "sql_evidence": {},
        "graph_evidence": {},
        "document_evidence": make_document_evidence(),
        "entity_ids": {},
        "entity_cross_references": {},
        "recommended_next_actions": [],
    }

    prompt = build_evaluation_prompt(
        context=context,
        answer_result={"answer_text": "Policy POL-000001 applies."},
    )

    assert re.findall(r"POL-\d+", prompt), (
        "the evaluator is asked to judge a claim about a policy ID it cannot see"
    )
