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
from opentelemetry.trace import Status, StatusCode

from otel import agent_span, record_llm_usage, set_attributes, workflow_span
from llm_usage import collect
from query_cache import cache_enabled, get_cache

class EnterpriseWorkflowState(TypedDict, total=False):
    query: str
    planned_route: dict[str, Any]
    post_retrieval_refusal: str | None
    assertion_failures: list[dict[str, Any]]
    replan_attempts: int
    replan_trigger: str | None
    replan_changes: list[str]
    first_attempt: dict[str, Any]
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



def query_planner_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    query = state["query"]
    run_id = state["run_id"]

    with trace_span(
        run_id=run_id,
        component="gemini_planner_agent",
        operation="plan_query_with_gemini",
        metadata={"query": query},
    ) as span, agent_span(run_id, "planner", query=query) as otel_span:
        with collect() as calls:
            planned_route = plan_query_with_gemini(query)

        record_llm_usage(otel_span, calls)
        set_attributes(
            otel_span,
            {
                "answerable": planned_route.get("answerable", True),
                "sql_intent": planned_route.get("sql_intent"),
                "graph_intent": planned_route.get("graph_intent"),
                "vector_groups": planned_route.get("vector_artifact_groups") or [],
                "dropped_filter_count": len(planned_route.get("dropped_filters") or []),
                "model": planned_route.get("model"),
            },
        )

        span["output"] = {
            "answerable": planned_route.get("answerable", True),
            "sql_intent": planned_route.get("sql_intent"),
            "graph_intent": planned_route.get("graph_intent"),
            "vector_artifact_groups": planned_route.get("vector_artifact_groups", []),
            "sql_filters": (planned_route.get("sql_plan") or {}).get("filters"),
            "sql_sort_by": (planned_route.get("sql_plan") or {}).get("sort_by"),
            "graph_filters": (planned_route.get("graph_plan") or {}).get("filters"),
            "dropped_filters": planned_route.get("dropped_filters", []),
            "model": planned_route.get("model"),
        }

    return {
        "planned_route": planned_route,
    }



POST_RETRIEVAL_GATE_NOTE = (
    "Measured on the M3 eval set: this gate converts 4 of the 10 'answered when it "
    "should have refused' items into refusals with zero false positives on the 53 "
    "answerable items. The 6 it does not catch (D57, D58, E67, E68, E75, E82) are not "
    "detectable after retrieval -- they are semantic or judgement failures, not "
    "evidence failures, and belong to the planner."
)


def post_retrieval_refusal(
    query: str,
    context: dict[str, Any],
    planned_route: dict[str, Any],
) -> str | None:
    """Decide, from the retrieved evidence alone, that the question cannot be answered.

    An empty or entity-less context must not reach the answer agent. When it did, the
    answer agent reported the absence in prose -- "there is no record of an order with
    that ID" -- which is honest but arrives as an answer rather than a refusal, so it
    is neither machine-checkable nor cheap.

    Every check here is deterministic and was validated against the M3 run for false
    positives before being enabled. A check that wrongly refuses an answerable question
    is worse than the behaviour it replaces.
    """
    summary = context.get("source_summary") or {}

    sql = summary.get("sql") or {}
    graph = summary.get("graph") or {}
    vector = summary.get("vector") or {}

    # --- 1. nothing came back from any leg
    total = (
        (sql.get("records") or 0)
        + (graph.get("records") or 0)
        + (vector.get("records") or 0)
    )

    if total == 0:
        return (
            "No evidence was found in any source for this question. The SQL, graph and "
            "document retrieval legs all returned zero records, so there is nothing to "
            "ground an answer on."
        )

    # --- 2. the question named an ID-shaped token that is not a valid identifier
    #
    # Only the exact reason "not a valid entity ID" is used. Broader drop reasons --
    # a value missing from a vocabulary -- fire on answerable questions too, because
    # the planner routinely proposes vocabulary values that do not resolve; enabling
    # them produced 12 false positives against 4 additional catches.
    invalid_ids = [
        d for d in (planned_route.get("dropped_filters") or [])
        if (d.get("reason") or "") == "not a valid entity ID"
    ]

    if invalid_ids:
        named = sorted({str(d.get("value")) for d in invalid_ids})
        return (
            f"The question names {', '.join(named)}, which is not a valid identifier in "
            "this system. The evidence retrieved is not about that entity, so no answer "
            "is given."
        )

    # --- 3. an artifact was named by id and the direct lookup found no such document
    lookups = vector.get("id_lookups") or []

    if lookups and not vector.get("id_lookup_hits"):
        named = ", ".join(f"{item['value']}" for item in lookups)
        return (
            f"No document with identifier {named} exists in the corpus. The direct "
            "lookup returned nothing, so the retrieved evidence is about other records."
        )

    return None


def refusal_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    """Terminal node for a plan that selected no retrieval route.

    "purple monkey dishwasher" used to raise ValueError here, because a plan with no
    routes was unrepresentable. It is now a first-class outcome: no database is
    queried, no answer is generated, and the refusal says so. The alternative --
    routing an unanswerable question to a plausible-looking template -- is exactly
    the F-03 behaviour where the control question and a business question came back
    with the same evidence.
    """
    planned_route = state["planned_route"]
    run_id = state["run_id"]

    reason = (
        state.get("post_retrieval_refusal")
        or planned_route.get("refusal_reason")
        or "The planner selected no retrieval route for this query."
    )

    with trace_span(
        run_id=run_id,
        component="hybrid_retrieval_tools",
        operation="refuse_unanswerable_query",
        metadata={"query": state["query"], "refusal_reason": reason},
    ) as span:
        span["output"] = {"answerable": False, "refusal_reason": reason}

    final_response = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_status": "REFUSED",
        "answerable": False,
        "refusal_reason": reason,
        "query": state["query"],
        "planned_route": planned_route,
        "source_summary": (state.get("retrieval_context") or {}).get(
            "source_summary",
            {
                "sql": {"intent": None, "records": 0, "skipped": True},
                "graph": {"intent": None, "records": 0, "skipped": True},
                "vector": {"artifact_groups": [], "records": 0, "skipped": True},
            },
        ),
        "refused_after_retrieval": bool(state.get("post_retrieval_refusal")),
        "evidence_quality": {
            "confidence": "none",
            "reasons": ["no retrieval route was selected; no evidence was gathered"],
        },
        "run_id": run_id,
        "answer_preview": reason,
    }

    return {"final_response": final_response}


def route_after_retrieval(state: EnterpriseWorkflowState) -> str:
    """An empty or entity-less context goes to the refusal node, not the answer agent."""
    return "refusal" if state.get("post_retrieval_refusal") else "answer"


def route_after_planning(state: EnterpriseWorkflowState) -> str:
    planned_route = state.get("planned_route") or {}

    if planned_route.get("answerable", True) is False:
        return "refusal"

    if not (
        planned_route.get("sql_intent")
        or planned_route.get("graph_intent")
        or planned_route.get("vector_artifact_groups")
    ):
        return "refusal"

    return "retrieve"


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
            run_id=run_id,
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
            "evidence_confidence": (context.get("evidence_quality") or {}).get(
                "confidence"
            ),
        }

    refusal_reason = post_retrieval_refusal(
        query=query,
        context=context,
        planned_route=state["planned_route"],
    )

    return {
        "retrieval_context": context,
        "post_retrieval_refusal": refusal_reason,
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
    ) as span, agent_span(run_id, "answer") as otel_span:
        with collect() as calls:
            result = generate_answer(
                context=context,
                gemini_model=settings["gemini_model"],
                gemini_api_key=settings["gemini_api_key"],
            )

        record_llm_usage(otel_span, calls)
        set_attributes(
            otel_span,
            {
                "answer_length_chars": len(result.get("answer_text", "")),
                "model": result.get("model"),
            },
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
    ) as span, agent_span(run_id, "evaluator") as otel_span:
        with collect() as calls:
            evaluation_result = evaluate_answer_with_gemini(
                context=context,
                answer_result=answer_result,
                gemini_model=settings["gemini_model"],
                gemini_api_key=settings["gemini_api_key"],
            )

        record_llm_usage(otel_span, calls)
        summary_for_span = evaluation_result.get("summary", {})
        set_attributes(
            otel_span,
            {
                "grounding_score": summary_for_span.get("grounding_score"),
                "completeness_score": summary_for_span.get("completeness_score"),
                "overall_status": summary_for_span.get("overall_status"),
                "unsupported_claim_count": len(
                    summary_for_span.get("unsupported_claims") or []
                ),
            },
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


# --------------------------------------------------------------------------------
# Evaluator-triggered replanning (M4 Task 2)
# --------------------------------------------------------------------------------
#
# MEASUREMENT NOTE, recorded next to the code because it governs how the feature
# should be read: on the M3 held-out set the evaluator scored 60 of 61 answers a 5
# and one a 2. The trigger therefore fires on exactly ONE item. That is not a
# property of this implementation -- it is the kappa 0.390 calibration finding
# showing up operationally. A near-constant judge cannot drive a feedback loop
# because it almost never reports failure. Any measured "recovery rate" here has
# n=1 and is not a measurement.

REPLAN_GROUNDING_THRESHOLD = 3
MAX_REPLAN_ATTEMPTS = 1

# Assertion types a broader retrieval could plausibly recover. A refusal-terms or
# injection failure cannot be fixed by widening the aperture, so retrying on those
# would spend tokens with no mechanism to help.
RETRYABLE_ASSERTIONS = ("required_numbers", "limit_awareness")

# SQL intents that carry the population-level columns a count/mean question needs.
# Empty for most intents: M3 recorded that NO template computes COUNT/AVG/MEDIAN over
# a population (README 23b), so for aggregate questions there is usually no adjacent
# template to switch to. Recorded here rather than pretending otherwise.
AGGREGATE_FALLBACK_INTENT = {
    "review_intelligence": "order_summary",
    "product_performance": "seller_performance",
}

# Adjacent artifact groups: the group most likely to carry the same case from a
# different angle. Used to broaden retrieval by exactly one group on retry.
ADJACENT_VECTOR_GROUPS = {
    "support_tickets": "customer_emails",
    "customer_emails": "support_tickets",
    "warranty_claims": "support_tickets",
    "logistics_incidents": "support_tickets",
    "policy_documents": "troubleshooting_guides",
    "troubleshooting_guides": "policy_documents",
}


def should_replan(
    evaluation_result: dict[str, Any],
    assertion_failures: list[dict[str, Any]] | None = None,
) -> tuple[bool, str | None]:
    """Combined trigger: judge signals OR deterministic assertion failures.

    The judge half fires on 1 of 61 items on the M3 set, because the evaluator is a
    near-constant function (kappa 0.390; 60 of 61 answers scored 5). The assertion
    half fires on 43 of 82 -- a 43x larger population -- and does so for stateable
    reasons rather than a model's opinion. M3 established the deterministic
    assertions as the primary reliability metric; this makes them drive the loop too.

    Only `required_numbers` and `limit_awareness` are wired in. The other assertion
    types are excluded deliberately: a refusal-terms failure or an injection failure
    is not something a broader retrieval can fix, so retrying on them would spend
    tokens with no mechanism to recover.
    """
    summary = evaluation_result.get("summary") or {}

    grounding = summary.get("grounding_score")
    unsupported = summary.get("unsupported_claims") or []

    if grounding is not None and grounding < REPLAN_GROUNDING_THRESHOLD:
        return True, f"grounding {grounding} below {REPLAN_GROUNDING_THRESHOLD}"

    if unsupported:
        return True, f"{len(unsupported)} unsupported claim(s) reported"

    for failure in assertion_failures or []:
        if failure.get("assertion") in RETRYABLE_ASSERTIONS:
            return True, f"assertion {failure['assertion']} failed"

    return False, None


def broaden_plan(
    planned_route: dict[str, Any],
    assertion_failures: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Widen the plan by one step. SQL and graph intents are deliberately unchanged.

    Changing the intent would be a different question, not a second attempt at this
    one. What is relaxed is the aperture: one more document group, and the single
    most restrictive numeric filter.
    """
    plan = json.loads(json.dumps(planned_route, default=str))
    changes: list[str] = []

    groups = list(plan.get("vector_artifact_groups") or [])

    for group in list(groups):
        adjacent = ADJACENT_VECTOR_GROUPS.get(group)

        if adjacent and adjacent not in groups:
            groups.append(adjacent)
            changes.append(f"added vector group {adjacent}")
            break

    if not groups:
        groups = ["support_tickets"]
        changes.append("added vector group support_tickets (plan had none)")

    plan["vector_artifact_groups"] = groups

    if plan.get("vector_plan"):
        plan["vector_plan"]["artifact_groups"] = groups

    sql_plan = plan.get("sql_plan") or {}
    filters = dict(sql_plan.get("filters") or {})

    if "max_avg_review_score" in filters:
        try:
            raised = float(filters["max_avg_review_score"]) + 0.5
            filters["max_avg_review_score"] = min(raised, 5.0)
            changes.append(
                f"raised max_avg_review_score to {filters['max_avg_review_score']}"
            )
        except (TypeError, ValueError):
            filters.pop("max_avg_review_score")
            changes.append("dropped unparseable max_avg_review_score")
    elif "min_late_delivery_rate" in filters:
        filters.pop("min_late_delivery_rate")
        changes.append("dropped min_late_delivery_rate floor")
    elif "min_orders" in filters:
        filters["min_orders"] = 1
        changes.append("lowered min_orders to 1")

    if sql_plan:
        sql_plan["filters"] = filters
        plan["sql_plan"] = sql_plan

    if not changes:
        changes.append("no broadening available; plan unchanged")

    failed = {f.get("assertion") for f in (assertion_failures or [])}

    # required_numbers: try the adjacent SQL template if one carries the needed
    # aggregate columns. Usually there is none -- see AGGREGATE_FALLBACK_INTENT.
    if "required_numbers" in failed:
        current = plan.get("sql_intent")
        fallback = AGGREGATE_FALLBACK_INTENT.get(current)

        if fallback:
            plan["sql_intent"] = fallback

            if plan.get("sql_plan"):
                plan["sql_plan"]["intent"] = fallback
                plan["sql_plan"]["filters"] = {}

            changes.append(f"switched SQL intent {current} -> {fallback} for aggregate coverage")
        else:
            changes.append(
                f"required_numbers failed but no adjacent aggregate template exists "
                f"for {current}; retrieval widened only"
            )

    # limit_awareness: the retrieval cannot be made to return a population, so the
    # instruction goes to the answer agent instead -- do not compute statistics from
    # the rows you were given.
    if "limit_awareness" in failed:
        plan["answer_directive"] = (
            "The rows below are a top-k sample, not the population. Do NOT compute or "
            "state any count, mean, median, proportion or total derived from them. If "
            "the question asks for a population statistic, say it cannot be computed "
            "from the retrieved sample."
        )
        changes.append("added answer directive: do not compute statistics from the sample")

    plan["replanned"] = True
    plan["replan_changes"] = changes

    return plan, changes


def replan_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    """Store the first attempt, broaden the plan, and go round once."""
    run_id = state["run_id"]
    evaluation_result = state.get("evaluation_result") or {}

    assertion_failures = state.get("assertion_failures") or []

    _, reason = should_replan(evaluation_result, assertion_failures)

    first_attempt = {
        "answer_result": state.get("answer_result"),
        "evaluation_result": evaluation_result,
        "retrieval_context": state.get("retrieval_context"),
        "planned_route": state.get("planned_route"),
        "grounding_score": (evaluation_result.get("summary") or {}).get("grounding_score"),
    }

    broadened, changes = broaden_plan(state["planned_route"], assertion_failures)

    with trace_span(
        run_id=run_id,
        component="replanner",
        operation="broaden_plan_and_retry",
        metadata={"query": state["query"], "trigger": reason},
    ) as span:
        span["output"] = {"changes": changes, "attempt": state.get("replan_attempts", 0) + 1}

    return {
        "planned_route": broadened,
        "replan_attempts": state.get("replan_attempts", 0) + 1,
        "replan_trigger": reason,
        "replan_changes": changes,
        "first_attempt": first_attempt,
    }


def route_after_evaluation(state: EnterpriseWorkflowState) -> str:
    if state.get("replan_attempts", 0) >= MAX_REPLAN_ATTEMPTS:
        return "final"

    triggered, _ = should_replan(
        state.get("evaluation_result") or {},
        state.get("assertion_failures") or [],
    )

    return "replan" if triggered else "final"


def final_response_node(state: EnterpriseWorkflowState) -> EnterpriseWorkflowState:
    context = state["retrieval_context"]
    answer_result = state["answer_result"]
    evaluation_result = state.get("evaluation_result", {})
    evaluation_summary = evaluation_result.get("summary", {})

    # If a retry happened, return whichever attempt scored better. A retry that made
    # the answer worse must not be shipped just because it came second.
    replan_note = None
    first = state.get("first_attempt")

    if first:
        first_score = first.get("grounding_score")
        second_score = evaluation_summary.get("grounding_score")

        def rank(value):
            return -1 if value is None else value

        if rank(first_score) > rank(second_score):
            context = first["retrieval_context"]
            answer_result = first["answer_result"]
            evaluation_result = first["evaluation_result"]
            evaluation_summary = evaluation_result.get("summary", {})
            replan_note = (
                f"Replanning was attempted (trigger: {state.get('replan_trigger')}) and "
                f"did not improve the answer (grounding {first_score} -> {second_score}). "
                "The original answer is returned."
            )
        else:
            replan_note = (
                f"Replanning was attempted (trigger: {state.get('replan_trigger')}); "
                f"grounding {first_score} -> {second_score}. "
                f"Changes: {'; '.join(state.get('replan_changes') or [])}."
            )
    

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
        "answerable": True,
        "refusal_reason": None,
        "evidence_quality": context.get("evidence_quality"),
        "unavailable_legs": context.get("unavailable_legs") or [],
        "degraded": bool(context.get("unavailable_legs")),
        "replan": {
            "attempted": bool(state.get("replan_attempts")),
            "trigger": state.get("replan_trigger"),
            "changes": state.get("replan_changes") or [],
            "first_grounding": (state.get("first_attempt") or {}).get("grounding_score"),
            "second_grounding": (
                (state.get("evaluation_result") or {}).get("summary") or {}
            ).get("grounding_score"),
            "note": replan_note,
        },
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
    workflow.add_node("refusal", refusal_node)
    workflow.add_node("retrieval_context_builder", retrieval_context_node)
    workflow.add_node("answer_generator", answer_generation_node)
    workflow.add_node("answer_evaluator", answer_evaluation_node)
    workflow.add_node("replanner", replan_node)
    workflow.add_node("final_response", final_response_node)

    workflow.add_edge(START, "query_planner")
    workflow.add_conditional_edges(
        "query_planner",
        route_after_planning,
        {"retrieve": "retrieval_context_builder", "refusal": "refusal"},
    )
    workflow.add_edge("refusal", END)
    workflow.add_conditional_edges(
        "retrieval_context_builder",
        route_after_retrieval,
        {"answer": "answer_generator", "refusal": "refusal"},
    )
    workflow.add_edge("answer_generator", "answer_evaluator")
    workflow.add_conditional_edges(
        "answer_evaluator",
        route_after_evaluation,
        {"replan": "replanner", "final": "final_response"},
    )
    workflow.add_edge("replanner", "retrieval_context_builder")
    workflow.add_edge("final_response", END)

    return workflow.compile()


def run_agentic_workflow(
    query: str,
    output_dir: Path,
    use_cache: bool | None = None,
) -> dict[str, Any]:
    """Run the pipeline, consulting the query cache first.

    A hit returns the stored response without touching a database or an LLM. The M5
    cost table puts an answered query at $0.00456 and ~15s, so a hit is worth all of
    it. Refusals are cached too -- they are a legitimate, reproducible outcome, and
    re-deriving one still costs a planner call.
    """
    caching = cache_enabled() if use_cache is None else use_cache
    cache = get_cache() if caching else None

    if cache is not None:
        cached = cache.get(query)

        if cached is not None:
            response = dict(cached)
            response["cache"] = {"hit": True, "backend": cache.backend.backend}
            return response

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

    run_id = initial_state["run_id"]

    with workflow_span(run_id, query) as root:
        final_state = workflow.invoke(initial_state)

        response_for_span = final_state.get("final_response") or {}
        set_attributes(
            root,
            {
                "overall_status": response_for_span.get("overall_status"),
                "answerable": response_for_span.get("answerable", True),
                "refused": response_for_span.get("answerable") is False,
                "degraded": bool(response_for_span.get("degraded")),
                "replan_attempted": bool(
                    (response_for_span.get("replan") or {}).get("attempted")
                ),
            },
        )

    final_response = final_state["final_response"]

    workflow_report_path = output_dir / "workflow_report.json"

    with workflow_report_path.open("w", encoding="utf-8") as file:
        json.dump(final_response, file, indent=2, ensure_ascii=False, default=str)

    # A refusal produces no answer, prompt or evaluation artefacts, so it carries no
    # output_files block of its own.
    final_response.setdefault("output_files", {})
    final_response["output_files"]["workflow_report"] = str(workflow_report_path)
    final_response["cache"] = {
        "hit": False,
        "backend": cache.backend.backend if cache is not None else None,
    }

    if cache is not None:
        cache.set(query, final_response)

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