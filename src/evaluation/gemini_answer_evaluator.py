from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT_FOR_USAGE = Path(__file__).resolve().parents[2]
_OBSERVABILITY_DIR = PROJECT_ROOT_FOR_USAGE / "src" / "observability"

if str(_OBSERVABILITY_DIR) not in sys.path:
    sys.path.append(str(_OBSERVABILITY_DIR))

from llm_usage import timed_call  # noqa: E402

from dotenv import load_dotenv


DEFAULT_CONTEXT_PATH = Path("reports/retrieval_context.json")
DEFAULT_ANSWER_JSON_PATH = Path("reports/generated_answer.json")
DEFAULT_EVALUATION_PATH = Path("reports/answer_evaluation.json")


def load_settings() -> dict[str, Any]:
    load_dotenv()

    return {
        "gemini_api_key": os.getenv("GEMINI_API_KEY"),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
    }


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def extract_json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1:
        raise ValueError(f"Could not find JSON object in Gemini response: {text}")

    json_text = cleaned[start : end + 1]
    return json.loads(json_text)


def compact_json(value: Any, max_chars: int = 12000) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)

    if len(text) <= max_chars:
        return text

    return text[:max_chars].strip() + "\n...TRUNCATED..."


def fit_document_evidence(
    document_evidence: dict[str, Any],
    max_chars: int,
) -> tuple[dict[str, Any], list[str]]:
    """Shrink document evidence to a budget WITHOUT losing an artifact group.

    The tail-chop in `compact_json` cut the last group out entirely. Measured on the
    flagship question: document evidence was 12,597 chars against a 9,000 budget, and
    all five `policy_documents` records -- the whole group -- fell off the end. The
    evaluator then reported the policy IDs in the answer as unsupported claims, which
    was correct from what it had been shown and wrong about the system.

    Groups are kept and trimmed evenly instead: long previews first, then records per
    group, never below one record per group. What was trimmed is returned so the
    prompt can say so, because the failure mode being fixed here is silent loss.
    """
    groups = document_evidence.get("results_by_artifact_group", {})

    if not groups:
        return document_evidence, []

    def rendered(candidate: dict[str, Any]) -> int:
        return len(json.dumps(candidate, indent=2, ensure_ascii=False, default=str))

    notes: list[str] = []

    for preview_length, records_per_group in (
        (None, None),
        (200, None),
        (120, None),
        (120, 3),
        (120, 2),
        (100, 1),
    ):
        trimmed_groups = {}

        for group, records in groups.items():
            kept = records if records_per_group is None else records[:records_per_group]
            trimmed_records = []

            for record in kept:
                item = dict(record)
                preview = item.get("text_preview")

                if preview_length is not None and isinstance(preview, str):
                    if len(preview) > preview_length:
                        item["text_preview"] = preview[:preview_length].strip() + "..."

                trimmed_records.append(item)

            trimmed_groups[group] = trimmed_records

        candidate = dict(document_evidence)
        candidate["results_by_artifact_group"] = trimmed_groups

        if rendered(candidate) <= max_chars:
            if preview_length is not None:
                notes.append(f"document previews shortened to {preview_length} chars")

            if records_per_group is not None:
                notes.append(
                    f"document evidence limited to {records_per_group} record(s) "
                    "per artifact group"
                )

            if notes:
                candidate["trimmed_for_evaluation"] = notes

            return candidate, notes

    # Still over budget at the tightest setting: keep every group, accept the size.
    candidate = dict(document_evidence)
    candidate["results_by_artifact_group"] = trimmed_groups
    notes.append(
        "document evidence exceeds the evaluation budget even at one record per "
        "group; every artifact group is still represented"
    )
    candidate["trimmed_for_evaluation"] = notes

    return candidate, notes


def build_evaluation_prompt(
    context: dict[str, Any],
    answer_result: dict[str, Any],
) -> str:
    source_summary = context.get("source_summary", {})
    answer_text = answer_result.get("answer_text", "")

    sql_evidence = context.get("sql_evidence", {})
    graph_evidence = context.get("graph_evidence", {})
    document_evidence, trim_notes = fit_document_evidence(
        context.get("document_evidence", {}),
        max_chars=9000,
    )
    entity_ids = context.get("entity_ids", {})
    entity_cross_references = context.get("entity_cross_references", {})
    recommended_next_actions = context.get("recommended_next_actions", [])

    prompt = f"""
You are an enterprise AI evaluation agent.

Your task is to evaluate whether the generated answer is grounded in the provided retrieval context.

You must not judge using outside knowledge.
Use only the retrieval context and generated answer.

Business question:
{context.get("business_question")}

Source summary:
{compact_json(source_summary, max_chars=3000)}

SQL evidence:
{compact_json(sql_evidence, max_chars=7000)}

Graph evidence:
{compact_json(graph_evidence, max_chars=7000)}

Document evidence:
{compact_json(document_evidence, max_chars=9000)}

Entity IDs:
{compact_json(entity_ids, max_chars=3000)}

Entities linked across sources (the same ID found in more than one retrieval leg):
{compact_json(entity_cross_references, max_chars=4000)}

Evidence trimming applied before this prompt (if any):
{compact_json(trim_notes, max_chars=800)}

Recommended next actions:
{compact_json(recommended_next_actions, max_chars=3000)}

Generated answer:
{answer_text}

Evaluate the answer using these rules:
- The answer should use SQL evidence for structured facts.
- The answer should use graph evidence for connected entity relationships.
- The answer should use document evidence for retrieved support tickets, policies, guides, incidents, emails, or warranty claims.
- The answer should not invent unsupported facts.
- An entity or document identifier that appears anywhere in the SQL evidence, graph
  evidence, document evidence or entity IDs above IS supported. Do not report such an
  identifier as an unsupported claim.
- The answer should include limitations if evidence is incomplete.
- The answer should be useful for a business user.
- Do not fail the answer only because it is cautious.
- Fail the answer if it contains unsupported factual claims, ignores major evidence, or gives unsafe overconfident conclusions.

Return ONLY valid JSON.
Do not include Markdown.
Do not include explanation outside JSON.

Required JSON schema:
{{
  "overall_status": "PASS or FAIL",
  "grounding_score": 1,
  "completeness_score": 1,
  "business_readiness_score": 1,
  "sql_evidence_used": true,
  "graph_evidence_used": true,
  "document_evidence_used": true,
  "limitations_mentioned": true,
  "unsupported_claims": [],
  "missing_evidence": [],
  "strengths": [],
  "improvement_suggestions": [],
  "evaluation_summary": "short summary"
}}

Scoring guide:
1 = poor
2 = weak
3 = acceptable
4 = good
5 = excellent
""".strip()

    return prompt


def call_gemini(prompt: str, model: str, api_key: str | None) -> str:
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is missing. Add GEMINI_API_KEY to your .env file."
        )

    try:
        from google import genai
    except ImportError as exc:
        raise ImportError(
            "google-genai is not installed. Run: pip install google-genai"
        ) from exc

    client = genai.Client(api_key=api_key)

    with timed_call("evaluator_agent", model, len(prompt)) as call:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        call["response"] = response

    response_text = getattr(response, "text", None)

    if not response_text or not response_text.strip():
        raise RuntimeError("Gemini returned an empty evaluator response.")

    return response_text.strip()


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "pass"}

    return bool(value)


def normalize_score(value: Any) -> int:
    try:
        score = int(value)
    except Exception:
        return 1

    return max(1, min(5, score))


def normalize_list(value: Any) -> list[Any]:
    if value is None:
        return []

    if isinstance(value, list):
        return value

    return [value]


def contains_any(text: str, terms: list[str]) -> bool:
    text_lower = text.lower()

    return any(term.lower() in text_lower for term in terms)


def run_deterministic_checks(
    context: dict[str, Any],
    answer_result: dict[str, Any],
) -> list[dict[str, Any]]:
    failed_checks = []

    answer_text = answer_result.get("answer_text", "")

    source_term_checks = {
        "sql_source_mentioned": ["PostgreSQL", "SQL evidence", "structured evidence", "structured records"],
        "graph_source_mentioned": ["Neo4j", "graph evidence", "connected records", "relationship evidence"],
        "document_source_mentioned": ["Qdrant", "document evidence", "semantic document", "retrieved documents"],
    }

    for check_name, accepted_terms in source_term_checks.items():
        if not contains_any(answer_text, accepted_terms):
            failed_checks.append(
                {
                    "check": check_name,
                    "expected": accepted_terms,
                    "actual": "none of the accepted terms found in answer",
                    "severity": "medium",
                }
            )

    if len(answer_text.strip()) < 1000:
        failed_checks.append(
            {
                "check": "answer_length",
                "expected": ">= 1000 characters",
                "actual": len(answer_text.strip()),
                "severity": "high",
            }
        )

    if not contains_any(answer_text, ["limitation", "limitations", "missing evidence", "not available", "not provided"]):
        failed_checks.append(
            {
                "check": "limitations_mentioned",
                "expected": "limitations or missing evidence mentioned",
                "actual": "missing from answer",
                "severity": "medium",
            }
        )

    return failed_checks


def normalize_evaluation(
    raw_evaluation: dict[str, Any],
    deterministic_failures: list[dict[str, Any]],
) -> dict[str, Any]:
    evaluator_status = str(raw_evaluation.get("overall_status", "FAIL")).upper()

    if evaluator_status not in {"PASS", "FAIL"}:
        evaluator_status = "FAIL"

    grounding_score = normalize_score(raw_evaluation.get("grounding_score"))
    completeness_score = normalize_score(raw_evaluation.get("completeness_score"))
    business_readiness_score = normalize_score(
        raw_evaluation.get("business_readiness_score")
    )

    unsupported_claims = normalize_list(raw_evaluation.get("unsupported_claims"))
    missing_evidence = normalize_list(raw_evaluation.get("missing_evidence"))
    strengths = normalize_list(raw_evaluation.get("strengths"))
    improvement_suggestions = normalize_list(
        raw_evaluation.get("improvement_suggestions")
    )

    high_severity_failures = [
        failure
        for failure in deterministic_failures
        if failure.get("severity") == "high"
    ]

    final_status = "PASS"

    if evaluator_status == "FAIL":
        final_status = "FAIL"

    if grounding_score < 3:
        final_status = "FAIL"

    if high_severity_failures:
        final_status = "FAIL"

    return {
        "overall_status": final_status,
        "evaluator_status": evaluator_status,
        "grounding_score": grounding_score,
        "completeness_score": completeness_score,
        "business_readiness_score": business_readiness_score,
        "sql_evidence_used": normalize_bool(raw_evaluation.get("sql_evidence_used")),
        "graph_evidence_used": normalize_bool(raw_evaluation.get("graph_evidence_used")),
        "document_evidence_used": normalize_bool(
            raw_evaluation.get("document_evidence_used")
        ),
        "limitations_mentioned": normalize_bool(
            raw_evaluation.get("limitations_mentioned")
        ),
        "unsupported_claims": unsupported_claims,
        "missing_evidence": missing_evidence,
        "strengths": strengths,
        "improvement_suggestions": improvement_suggestions,
        "deterministic_failures": deterministic_failures,
        "evaluation_summary": str(raw_evaluation.get("evaluation_summary", "")),
    }


def evaluate_answer_with_gemini(
    context: dict[str, Any],
    answer_result: dict[str, Any],
    gemini_model: str,
    gemini_api_key: str | None,
) -> dict[str, Any]:
    prompt = build_evaluation_prompt(
        context=context,
        answer_result=answer_result,
    )

    response_text = call_gemini(
        prompt=prompt,
        model=gemini_model,
        api_key=gemini_api_key,
    )

    raw_evaluation = extract_json_from_text(response_text)

    deterministic_failures = run_deterministic_checks(
        context=context,
        answer_result=answer_result,
    )

    normalized = normalize_evaluation(
        raw_evaluation=raw_evaluation,
        deterministic_failures=deterministic_failures,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "gemini",
        "model": gemini_model,
        "business_question": context.get("business_question"),
        "summary": normalized,
        "raw_evaluation": raw_evaluation,
        "raw_response": response_text,
        "prompt": prompt,
    }


def save_evaluation_output(
    evaluation_result: dict[str, Any],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(evaluation_result, file, indent=2, ensure_ascii=False, default=str)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a generated answer against retrieval context using Gemini."
    )

    parser.add_argument(
        "--context",
        type=Path,
        default=DEFAULT_CONTEXT_PATH,
        help="Path to retrieval_context.json.",
    )

    parser.add_argument(
        "--answer-json",
        type=Path,
        default=DEFAULT_ANSWER_JSON_PATH,
        help="Path to generated_answer.json.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_EVALUATION_PATH,
        help="Path to save answer evaluation JSON.",
    )

    parser.add_argument(
        "--gemini-model",
        type=str,
        default=None,
        help="Gemini model name. Defaults to GEMINI_MODEL env value.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()

    gemini_model = args.gemini_model or settings["gemini_model"]

    print(f"Loading retrieval context: {args.context}")
    context = load_json(args.context)

    print(f"Loading generated answer: {args.answer_json}")
    answer_result = load_json(args.answer_json)

    print(f"Evaluating answer using Gemini model: {gemini_model}")
    evaluation_result = evaluate_answer_with_gemini(
        context=context,
        answer_result=answer_result,
        gemini_model=gemini_model,
        gemini_api_key=settings["gemini_api_key"],
    )

    save_evaluation_output(
        evaluation_result=evaluation_result,
        output_path=args.output,
    )

    summary = evaluation_result["summary"]

    print("\nGemini Answer Evaluation Completed")
    print("----------------------------------")
    print(f"Overall status: {summary['overall_status']}")
    print(f"Evaluator status: {summary['evaluator_status']}")
    print(f"Grounding score: {summary['grounding_score']}")
    print(f"Completeness score: {summary['completeness_score']}")
    print(f"Business readiness score: {summary['business_readiness_score']}")
    print(f"SQL evidence used: {summary['sql_evidence_used']}")
    print(f"Graph evidence used: {summary['graph_evidence_used']}")
    print(f"Document evidence used: {summary['document_evidence_used']}")
    print(f"Limitations mentioned: {summary['limitations_mentioned']}")
    print(f"Unsupported claims: {len(summary['unsupported_claims'])}")
    print(f"Deterministic failures: {len(summary['deterministic_failures'])}")
    print(f"Evaluation saved to: {args.output}")

    print("\nEvaluation summary:")
    print(summary["evaluation_summary"])


if __name__ == "__main__":
    main()