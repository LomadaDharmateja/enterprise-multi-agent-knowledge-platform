from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_DIR = PROJECT_ROOT / "src" / "retrieval"
GENERATION_DIR = PROJECT_ROOT / "src" / "generation"

if str(RETRIEVAL_DIR) not in sys.path:
    sys.path.append(str(RETRIEVAL_DIR))

if str(GENERATION_DIR) not in sys.path:
    sys.path.append(str(GENERATION_DIR))


from retrieval_context_builder import build_retrieval_context
from answer_generator import generate_answer, save_outputs, load_settings


DEFAULT_QUERY = (
    "Find sellers with negative customer complaints, warranty issues, "
    "and relevant support policies"
)


def validate_generated_answer(result: dict[str, Any]) -> list[dict[str, Any]]:
    failed_checks = []

    answer_text = result.get("answer_text", "")

    if result.get("provider") != "gemini":
        failed_checks.append(
            {
                "check": "provider",
                "expected": "gemini",
                "actual": result.get("provider"),
            }
        )

    if not str(result.get("model", "")).startswith("gemini"):
        failed_checks.append(
            {
                "check": "model",
                "expected": "Gemini model",
                "actual": result.get("model"),
            }
        )

    if len(answer_text.strip()) < 1000:
        failed_checks.append(
            {
                "check": "answer_length",
                "expected": ">= 1000 characters",
                "actual": len(answer_text.strip()),
            }
        )

    required_sections = [
        "Business Question",
        "Executive Answer",
        "Evidence",
    ]

    for section in required_sections:
        if section.lower() not in answer_text.lower():
            failed_checks.append(
                {
                    "check": "required_section",
                    "expected": section,
                    "actual": "missing from answer",
                }
            )

    evidence_terms = [
        "SQL",
        "graph",
        "document",
    ]

    for term in evidence_terms:
        if term.lower() not in answer_text.lower():
            failed_checks.append(
                {
                    "check": "evidence_term",
                    "expected": term,
                    "actual": "missing from answer",
                }
            )

    return failed_checks


def run_answer_generation_validation(
    query: str,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_retrieval_path = output_dir / "answer_generation_raw_retrieval.json"
    context_output_path = output_dir / "answer_generation_retrieval_context.json"
    answer_json_path = output_dir / "answer_generation_answer.json"
    answer_md_path = output_dir / "answer_generation_answer.md"
    prompt_path = output_dir / "answer_generation_prompt.txt"

    settings = load_settings()

    context = build_retrieval_context(
        query=query,
        raw_report_path=raw_retrieval_path,
        context_output_path=context_output_path,
        sql_limit=10,
        graph_limit=10,
        vector_limit=5,
        max_records_per_section=5,
    )

    result = generate_answer(
        context=context,
        gemini_model=settings["gemini_model"],
        gemini_api_key=settings["gemini_api_key"],
    )

    save_outputs(
        result=result,
        answer_json_path=answer_json_path,
        answer_md_path=answer_md_path,
        prompt_path=prompt_path,
    )

    failed_checks = validate_generated_answer(result)

    status = "PASS" if not failed_checks else "FAIL"

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": status,
        "query": query,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "answer_length_chars": len(result.get("answer_text", "")),
        "failed_checks": failed_checks,
        "output_files": {
            "raw_retrieval": str(raw_retrieval_path),
            "retrieval_context": str(context_output_path),
            "answer_json": str(answer_json_path),
            "answer_markdown": str(answer_md_path),
            "prompt": str(prompt_path),
        },
    }

    report_path = output_dir / "answer_generation_validation_report.json"

    with report_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False, default=str)

    report["output_files"]["validation_report"] = str(report_path)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Gemini grounded answer generation."
    )

    parser.add_argument(
        "--query",
        type=str,
        default=DEFAULT_QUERY,
        help="Business query to validate.",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/answer_generation"),
        help="Output directory for validation artifacts.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = run_answer_generation_validation(
        query=args.query,
        output_dir=args.output_dir,
    )

    print("\nGemini Grounded Answer Generation Validation Completed")
    print("-----------------------------------------------------")
    print(f"Overall status: {report['overall_status']}")
    print(f"Provider: {report['provider']}")
    print(f"Model: {report['model']}")
    print(f"Answer length chars: {report['answer_length_chars']}")
    print(f"Report saved to: {report['output_files']['validation_report']}")

    if report["failed_checks"]:
        print("\nFailed checks:")

        for failed_check in report["failed_checks"]:
            print(
                f"- {failed_check['check']}: "
                f"expected={failed_check['expected']} "
                f"actual={failed_check['actual']}"
            )


if __name__ == "__main__":
    main()