from __future__ import annotations

import argparse
import json
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


DEFAULT_CONTEXT_PATH = Path("reports/retrieval_context.json")
DEFAULT_ANSWER_JSON_PATH = Path("reports/generated_answer.json")
DEFAULT_ANSWER_MD_PATH = Path("reports/generated_answer.md")
DEFAULT_PROMPT_PATH = Path("reports/generated_answer_prompt.txt")


def load_settings() -> dict[str, Any]:
    load_dotenv()

    return {
        "gemini_api_key": os.getenv("GEMINI_API_KEY"),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
    }


def load_context(context_path: Path) -> dict[str, Any]:
    if not context_path.exists():
        raise FileNotFoundError(f"Retrieval context file not found: {context_path}")

    with context_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def truncate_text(value: Any, max_length: int = 500) -> str:
    if value is None:
        return ""

    text = str(value)

    if len(text) <= max_length:
        return text

    return text[:max_length].strip() + "..."


def format_value(value: Any) -> str:
    if value is None:
        return "N/A"

    if isinstance(value, float):
        return f"{value:.4f}"

    return str(value)


def format_record(record: dict[str, Any], fields: list[str]) -> str:
    parts = []

    for field in fields:
        if field not in record:
            continue

        value = record.get(field)

        if value is None:
            continue

        parts.append(f"{field}={format_value(value)}")

    return "; ".join(parts)


def extract_top_records(records: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    return records[:limit]


def build_sql_summary(context: dict[str, Any]) -> list[str]:
    sql_evidence = context["sql_evidence"]
    records = extract_top_records(sql_evidence["evidence"], limit=5)

    lines = [
        "SQL source: PostgreSQL",
        f"SQL intent: {sql_evidence['intent']}",
        f"Structured records returned: {sql_evidence['total_records_returned']}",
    ]

    if not records:
        lines.append("No SQL evidence records were available.")
        return lines

    lines.append("Top SQL evidence:")

    preferred_fields = [
        "seller_id",
        "seller_state",
        "seller_city",
        "total_orders",
        "total_items_sold",
        "total_item_revenue",
        "avg_review_score",
        "review_count",
        "late_delivery_orders",
        "order_id",
        "customer_id",
        "customer_state",
        "customer_city",
        "order_status",
        "delivery_status",
        "is_late_delivery",
        "delivery_delay_days",
        "total_payment_value",
        "review_score",
        "sentiment_label",
        "is_negative_review",
        "payment_types",
        "max_payment_installments",
        "product_id",
        "product_category_name_english",
        "total_product_revenue",
    ]

    for index, record in enumerate(records, start=1):
        lines.append(f"{index}. {format_record(record, preferred_fields)}")

    return lines


def build_graph_summary(context: dict[str, Any]) -> list[str]:
    graph_evidence = context["graph_evidence"]
    records = extract_top_records(graph_evidence["evidence"], limit=5)

    lines = [
        "Graph source: Neo4j",
        f"Graph intent: {graph_evidence['intent']}",
        f"Graph records returned: {graph_evidence['total_records_returned']}",
    ]

    if not records:
        lines.append("No graph evidence records were available.")
        return lines

    lines.append("Top graph evidence:")

    preferred_fields = [
        "seller_id",
        "seller_state",
        "ticket_id",
        "issue_type",
        "severity",
        "claim_id",
        "claim_status",
        "incident_id",
        "incident_type",
        "order_id",
        "delivery_delay_days",
        "product_id",
        "category",
        "category_id",
        "region_state",
        "region_city",
        "policy_document_id",
        "policy_title",
        "guide_id",
        "guide_title",
        "customer_id",
        "customer_state",
    ]

    for index, record in enumerate(records, start=1):
        lines.append(f"{index}. {format_record(record, preferred_fields)}")

    return lines


def build_document_summary(context: dict[str, Any]) -> list[str]:
    document_evidence = context["document_evidence"]
    grouped_results = document_evidence["results_by_artifact_group"]

    lines = [
        "Document source: Qdrant",
        f"Artifact groups: {document_evidence['artifact_groups']}",
        f"Document records returned: {document_evidence['total_records_returned']}",
    ]

    if not grouped_results:
        lines.append("No document evidence records were available.")
        return lines

    lines.append("Top document evidence:")

    for artifact_group, records in grouped_results.items():
        lines.append(f"- {artifact_group}:")

        if not records:
            lines.append("  No documents retrieved.")
            continue

        for index, record in enumerate(records[:3], start=1):
            title = truncate_text(record.get("title"), 140)
            score = record.get("score")
            artifact_id = record.get("artifact_id")
            issue_type = record.get("issue_type")
            severity = record.get("severity")
            status = record.get("status")
            policy_topic = record.get("policy_topic")
            preview = truncate_text(record.get("text_preview"), 350)

            lines.append(
                f"  {index}. artifact_id={artifact_id}; "
                f"score={format_value(score)}; "
                f"title={title}; "
                f"issue_type={issue_type}; "
                f"severity={severity}; "
                f"status={status}; "
                f"policy_topic={policy_topic}; "
                f"preview={preview}"
            )

    return lines


def build_entity_summary(context: dict[str, Any]) -> list[str]:
    entity_ids = context.get("entity_ids", {})

    if not entity_ids:
        return ["No entity IDs were extracted."]

    lines = ["Key entity groups:"]

    for entity_name, values in entity_ids.items():
        limited_values = values[:15]
        lines.append(f"- {entity_name}: {', '.join(limited_values)}")

    return lines


def build_recommended_action_summary(context: dict[str, Any]) -> list[str]:
    actions = context.get("recommended_next_actions", [])

    if not actions:
        return ["No recommended actions were generated by the retrieval layer."]

    return [f"- {action}" for action in actions]


def build_grounded_prompt(context: dict[str, Any]) -> str:
    sql_summary = "\n".join(build_sql_summary(context))
    graph_summary = "\n".join(build_graph_summary(context))
    document_summary = "\n".join(build_document_summary(context))
    entity_summary = "\n".join(build_entity_summary(context))
    next_actions = "\n".join(build_recommended_action_summary(context))

    prompt = f"""
You are an enterprise AI assistant for a hybrid knowledge intelligence platform.

You must answer the business question using ONLY the provided retrieval context.

Important grounding rules:
- Do not invent facts.
- Do not use outside knowledge.
- Do not assume missing values.
- If evidence is insufficient, clearly say what is missing.
- Use SQL evidence for structured business facts.
- Use Neo4j evidence for connected business relationships.
- Use Qdrant evidence for semantically retrieved enterprise documents.
- Mention important entity IDs when useful.
- Keep the answer business-ready and practical.
- Do not claim that a seller, customer, product, or policy is risky unless the provided evidence supports it.
- Prefer cautious language such as "the retrieved evidence indicates" or "based on the retrieved records".

Business question:
{context["business_question"]}

SQL evidence:
{sql_summary}

Graph evidence:
{graph_summary}

Document evidence:
{document_summary}

Entity evidence:
{entity_summary}

Recommended next actions from retrieval layer:
{next_actions}

Produce the final answer in Markdown with exactly these sections:

# Grounded Business Answer

## Business Question

## Executive Answer

## Evidence Used

### SQL Evidence

### Graph Evidence

### Document Evidence

## Key Entities

## Recommended Next Actions

## Limitations
""".strip()

    return textwrap.dedent(prompt)


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

    response = client.models.generate_content(
        model=model,
        contents=prompt,
    )

    answer_text = getattr(response, "text", None)

    if not answer_text or not answer_text.strip():
        raise RuntimeError("Gemini returned an empty response.")

    return answer_text.strip()


def generate_answer(
    context: dict[str, Any],
    gemini_model: str,
    gemini_api_key: str | None,
) -> dict[str, Any]:
    prompt = build_grounded_prompt(context)

    answer_text = call_gemini(
        prompt=prompt,
        model=gemini_model,
        api_key=gemini_api_key,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "gemini",
        "model": gemini_model,
        "business_question": context["business_question"],
        "summary": {
            "overall_status": "PASS",
            "answer_length_chars": len(answer_text),
            "uses_sql_evidence": True,
            "uses_graph_evidence": True,
            "uses_document_evidence": True,
        },
        "source_summary": context["source_summary"],
        "answer_text": answer_text,
        "prompt": prompt,
    }


def save_outputs(
    result: dict[str, Any],
    answer_json_path: Path,
    answer_md_path: Path,
    prompt_path: Path,
) -> None:
    answer_json_path.parent.mkdir(parents=True, exist_ok=True)
    answer_md_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)

    with answer_json_path.open("w", encoding="utf-8") as file:
        json.dump(result, file, indent=2, ensure_ascii=False, default=str)

    with answer_md_path.open("w", encoding="utf-8") as file:
        file.write(result["answer_text"])

    with prompt_path.open("w", encoding="utf-8") as file:
        file.write(result["prompt"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a grounded Gemini business answer from retrieval context."
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
        help="Path to save generated answer JSON.",
    )

    parser.add_argument(
        "--answer-md",
        type=Path,
        default=DEFAULT_ANSWER_MD_PATH,
        help="Path to save generated answer Markdown.",
    )

    parser.add_argument(
        "--prompt-output",
        type=Path,
        default=DEFAULT_PROMPT_PATH,
        help="Path to save grounded prompt.",
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
    context = load_context(args.context)

    print(f"Generating grounded answer using Gemini model: {gemini_model}")
    result = generate_answer(
        context=context,
        gemini_model=gemini_model,
        gemini_api_key=settings["gemini_api_key"],
    )

    save_outputs(
        result=result,
        answer_json_path=args.answer_json,
        answer_md_path=args.answer_md,
        prompt_path=args.prompt_output,
    )

    print("\nGemini Grounded Answer Generation Completed")
    print("-------------------------------------------")
    print(f"Overall status: {result['summary']['overall_status']}")
    print(f"Provider: {result['provider']}")
    print(f"Model: {result['model']}")
    print(f"Answer length chars: {result['summary']['answer_length_chars']}")
    print(f"Answer JSON saved to: {args.answer_json}")
    print(f"Answer Markdown saved to: {args.answer_md}")
    print(f"Prompt saved to: {args.prompt_output}")

    print("\nAnswer preview:")
    print(result["answer_text"][:1500])


if __name__ == "__main__":
    main()