from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_DIR = PROJECT_ROOT / "src" / "retrieval"
GENERATION_DIR = PROJECT_ROOT / "src" / "generation"
PLANNING_DIR = PROJECT_ROOT / "src" / "planning"
EVALUATION_DIR = PROJECT_ROOT / "src" / "evaluation"
OBSERVABILITY_DIR = PROJECT_ROOT / "src" / "observability"


if str(RETRIEVAL_DIR) not in sys.path:
    sys.path.append(str(RETRIEVAL_DIR))

if str(GENERATION_DIR) not in sys.path:
    sys.path.append(str(GENERATION_DIR))

if str(EVALUATION_DIR) not in sys.path:
    sys.path.append(str(EVALUATION_DIR))

if str(PLANNING_DIR) not in sys.path:
    sys.path.append(str(PLANNING_DIR))

if str(OBSERVABILITY_DIR) not in sys.path:
    sys.path.append(str(OBSERVABILITY_DIR))

from gemini_answer_evaluator import (
    evaluate_answer_with_gemini,
    save_evaluation_output,
    load_settings as load_evaluation_settings,
)

from langgraph.graph import END, START, StateGraph
from gemini_query_planner import plan_query_with_gemini
from retrieval_context_builder import build_retrieval_context
from answer_generator import generate_answer, save_outputs, load_settings
from observability import new_run_id, record_event, trace_span

class EnterpriseWorkflowState(TypedDict, total=False):
    query: str
    planned_route: dict[str, Any]
    raw_retrieval_path: str
    context_path: str
    answer_json_path: str
    answer_md_path: str
    prompt_path: str
    retrieval_context: dict[str, Any]
    answer_result: dict[str, Any]
    final_response: dict[str, Any]
    errors: list[str]
    evaluation_json_path: str
    evaluation_result: dict[str, Any]
    run_id: str
    retrieval_context_path: str


def normalize_query(query: str) -> str:
    return query.lower().strip()


def plan_query_route(query: str) -> dict[str, Any]:
    q = normalize_query(query)

    route_signals = {
        "mentions_seller": any(term in q for term in ["seller", "vendor"]),
        "mentions_customer": any(term in q for term in ["customer", "complaint", "support"]),
        "mentions_delivery": any(term in q for term in ["delivery", "delay", "late", "logistics", "shipment"]),
        "mentions_warranty": any(term in q for term in ["warranty", "claim", "replacement"]),
        "mentions_policy": any(term in q for term in ["policy", "policies", "refund", "rule", "rules"]),
        "mentions_troubleshooting": any(term in q for term in ["troubleshooting", "guide", "guidance", "procedure"]),
        "mentions_payment": any(term in q for term in ["payment", "installment", "refund", "charge"]),
        "mentions_product_quality": any(term in q for term in ["product", "quality", "damaged", "issue"]),
    }

    selected_capabilities = [
        "sql_retrieval",
        "graph_retrieval",
        "vector_retrieval",
        "context_building",
        "grounded_answer_generation",
    ]

    return {
        "routing_mode": "hybrid_retrieval_workflow",
        "route_signals": route_signals,
        "selected_capabilities": selected_capabilities,
        "notes": (
            "This planner currently records query signals and delegates final SQL, graph, "
            "and vector routing to the validated hybrid retriever."
        ),
    }


def query_planner_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    query = state["query"]
    run_id = state["run_id"]

    with trace_span(
        run_id=run_id,
        component="gemini_planner_agent",
        operation="plan_query_route",
        metadata={"query": query},
    ) as span:
        planned_route = plan_query_with_gemini(query)

        span["output"] = {
            "sql_intent": planned_route.get("sql_intent"),
            "graph_intent": planned_route.get("graph_intent"),
            "vector_artifact_groups": planned_route.get("vector_artifact_groups", []),
            "model": planned_route.get("model"),
        }

    return {
        "planned_route": planned_route,
    }


def retrieval_context_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    query = state["query"]
    run_id = state["run_id"]

    raw_retrieval_path = Path(state["raw_retrieval_path"])
    context_path = Path(state["retrieval_context_path"])

    with trace_span(
        run_id=run_id,
        component="hybrid_retrieval_tools",
        operation="build_retrieval_context",
        metadata={
            "query": query,
            "route_plan": state["planned_route"],
        },
    ) as span:
        context = build_retrieval_context(
            query=query,
            raw_report_path=raw_retrieval_path,
            context_output_path=context_path,
            sql_limit=10,
            graph_limit=10,
            vector_limit=5,
            max_records_per_section=5,
            route_plan=state["planned_route"],
        )

        source_summary = context.get("source_summary", {})

        span["output"] = {
            "sql_intent": source_summary.get("sql", {}).get("intent"),
            "graph_intent": source_summary.get("graph", {}).get("intent"),
            "vector_artifact_groups": source_summary.get("vector", {}).get(
                "artifact_groups", []
            ),
            "sql_records": source_summary.get("sql", {}).get("record_count"),
            "graph_records": source_summary.get("graph", {}).get("record_count"),
            "vector_records": source_summary.get("vector", {}).get("record_count"),
        }

    return {
        "retrieval_context": context,
    }

def answer_generation_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    context = state["retrieval_context"]
    run_id = state["run_id"]

    settings = load_settings()

    with trace_span(
        run_id=run_id,
        component="gemini_answer_agent",
        operation="generate_grounded_answer",
        metadata={
            "business_question": context.get("business_question"),
            "model": settings["gemini_model"],
        },
    ) as span:
        result = generate_answer(
            context=context,
            gemini_model=settings["gemini_model"],
            gemini_api_key=settings["gemini_api_key"],
        )

        save_outputs(
            result=result,
            answer_json_path=Path(state["answer_json_path"]),
            answer_md_path=Path(state["answer_md_path"]),
            prompt_path=Path(state["prompt_path"]),
        )

        span["output"] = {
            "provider": result.get("provider"),
            "model": result.get("model"),
            "answer_length_chars": len(result.get("answer_text", "")),
        }

    return {
        "answer_result": result,
    }

def answer_evaluation_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    context = state["retrieval_context"]
    answer_result = state["answer_result"]
    run_id = state["run_id"]

    settings = load_evaluation_settings()

    with trace_span(
        run_id=run_id,
        component="gemini_evaluation_agent",
        operation="evaluate_answer_grounding",
        metadata={
            "business_question": context.get("business_question"),
            "model": settings["gemini_model"],
        },
    ) as span:
        evaluation_result = evaluate_answer_with_gemini(
            context=context,
            answer_result=answer_result,
            gemini_model=settings["gemini_model"],
            gemini_api_key=settings["gemini_api_key"],
        )

        save_evaluation_output(
            evaluation_result=evaluation_result,
            output_path=Path(state["evaluation_json_path"]),
        )

        evaluation_summary = evaluation_result.get("summary", {})

        span["output"] = {
            "overall_status": evaluation_summary.get("overall_status"),
            "grounding_score": evaluation_summary.get("grounding_score"),
            "completeness_score": evaluation_summary.get("completeness_score"),
            "business_readiness_score": evaluation_summary.get(
                "business_readiness_score"
            ),
            "unsupported_claims": len(
                evaluation_summary.get("unsupported_claims", [])
            ),
            "deterministic_failures": len(
                evaluation_summary.get("deterministic_failures", [])
            ),
        }

    return {
        "evaluation_result": evaluation_result,
    }

def final_response_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    context = state["retrieval_context"]
    answer_result = state["answer_result"]
    evaluation_result = state.get("evaluation_result", {})
    evaluation_summary = evaluation_result.get("summary", {})
    

    final_response = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": evaluation_summary.get("overall_status", "PASS"),
        "query": state["query"],
        "planned_route": state["planned_route"],
        "source_summary": context["source_summary"],
        "answer_provider": answer_result["provider"],
        "answer_model": answer_result["model"],
        "answer_length_chars": answer_result["summary"]["answer_length_chars"],
        "evaluation_summary": evaluation_summary,
        "run_id": state["run_id"],
        "output_files": {
            "raw_retrieval": state["raw_retrieval_path"],
            "retrieval_context": state["context_path"],
            "answer_json": state["answer_json_path"],
            "answer_markdown": state["answer_md_path"],
            "prompt": state["prompt_path"],
            "evaluation_json": state["evaluation_json_path"],
            "observability_events": "reports/observability/workflow_events.jsonl",
            "observability_metrics": "reports/observability/workflow_metrics.csv",
        },
        "answer_preview": answer_result.get("answer_text", ""),
    }

    return {
        "final_response": final_response,
    }


def build_enterprise_workflow():
    workflow = StateGraph(EnterpriseWorkflowState)

    workflow.add_node("query_planner", query_planner_node)
    workflow.add_node("retrieval_context_builder", retrieval_context_node)
    workflow.add_node("answer_generator", answer_generation_node)
    workflow.add_node("answer_evaluator", answer_evaluation_node)
    workflow.add_node("final_response", final_response_node)

    workflow.add_edge(START, "query_planner")
    workflow.add_edge("query_planner", "retrieval_context_builder")
    workflow.add_edge("retrieval_context_builder", "answer_generator")
    workflow.add_edge("answer_generator", "answer_evaluator")
    workflow.add_edge("answer_evaluator", "final_response")
    workflow.add_edge("final_response", END)

    return workflow.compile()


def run_agentic_workflow(
    query: str,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    workflow = build_enterprise_workflow()

    initial_state: EnterpriseWorkflowState = {
        "query": query,
        "run_id": new_run_id("agentic_workflow"),
        "raw_retrieval_path": str(output_dir / "workflow_raw_retrieval.json"),
        "context_path": str(output_dir / "workflow_retrieval_context.json"),
        "answer_json_path": str(output_dir / "workflow_answer.json"),
        "answer_md_path": str(output_dir / "workflow_answer.md"),
        "prompt_path": str(output_dir / "workflow_prompt.txt"),
        "evaluation_json_path": str(output_dir / "workflow_answer_evaluation.json"),
        "retrieval_context_path": str(output_dir / "workflow_retrieval_context.json"),
        "errors": [],
    }

    final_state = workflow.invoke(initial_state)

    final_response = final_state["final_response"]

    workflow_report_path = output_dir / "workflow_report.json"

    with workflow_report_path.open("w", encoding="utf-8") as file:
        json.dump(final_response, file, indent=2, ensure_ascii=False, default=str)

    final_response["output_files"]["workflow_report"] = str(workflow_report_path)

    return final_response


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the enterprise agentic workflow."
    )

    parser.add_argument(
        "--query",
        type=str,
        required=True,
        help="Natural-language business query.",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/agentic_workflow"),
        help="Directory to save workflow outputs.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("Running enterprise agentic workflow...")
    result = run_agentic_workflow(
        query=args.query,
        output_dir=args.output_dir,
    )

    print("\nEnterprise Agentic Workflow Completed")
    print("-------------------------------------")
    print(f"Overall status: {result['overall_status']}")
    print(f"Query: {result['query']}")
    print(f"Answer provider: {result['answer_provider']}")
    print(f"Answer model: {result['answer_model']}")
    print(f"Answer length chars: {result['answer_length_chars']}")
    evaluation_summary = result.get("evaluation_summary", {})

    if evaluation_summary:
        print("\nEvaluation summary:")
        print(f"Evaluation status: {evaluation_summary.get('overall_status')}")
        print(f"Grounding score: {evaluation_summary.get('grounding_score')}")
        print(f"Completeness score: {evaluation_summary.get('completeness_score')}")
        print(f"Business readiness score: {evaluation_summary.get('business_readiness_score')}")
        print(f"Unsupported claims: {len(evaluation_summary.get('unsupported_claims', []))}")
        print(f"Deterministic failures: {len(evaluation_summary.get('deterministic_failures', []))}")

    print("\nSource summary:")
    print(f"SQL intent: {result['source_summary']['sql']['intent']}")
    print(f"Graph intent: {result['source_summary']['graph']['intent']}")
    print(f"Vector groups: {result['source_summary']['vector']['artifact_groups']}")

    print("\nOutput files:")
    for label, path in result["output_files"].items():
        print(f"{label}: {path}")

    print("\nAnswer preview:")
    print(result["answer_preview"])


if __name__ == "__main__":
    main()