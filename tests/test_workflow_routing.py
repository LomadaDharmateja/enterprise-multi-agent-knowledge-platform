"""End-to-end routing behaviour: refusals, SQL-only plans, and static templates.

The Gemini-dependent tests are marked `requires_gemini` and are non-deterministic by
nature -- the planner is an LLM. They assert the property the M2 exit criterion
actually states ("a refusal OR different evidence"), not the outcome we would prefer.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

RETRIEVER = Path(__file__).resolve().parents[1] / "src" / "retrieval" / "hybrid_retriever.py"

FLAGSHIP = (
    "Find sellers with negative customer complaints, warranty issues, "
    "and relevant support policies"
)
CONTROL = "purple monkey dishwasher"
REVENUE = "Which seller had the highest revenue?"


# --------------------------------------------------------------------------------
# D-3: the templates must stay static literals
# --------------------------------------------------------------------------------


def test_templates_are_static_literals_not_assembled_strings():
    """No SQL or Cypher string may be built at runtime.

    Parameterisation is what F-03 needs; string assembly is how it would be done
    badly. Every template stays a plain literal so the allowlist freeze test can
    reach it and so no model-derived text can ever enter a query.
    """
    tree = ast.parse(RETRIEVER.read_text(encoding="utf-8"))

    offenders = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in {"sql_retrieve", "graph_retrieve"}):
            continue

        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in {"sql_templates", "cypher_templates"}
                for t in stmt.targets
            ):
                assert isinstance(stmt.value, ast.Dict)

                for key, value in zip(stmt.value.keys, stmt.value.values):
                    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                        offenders.append(
                            f"{node.name}[{getattr(key, 'value', '?')}] is "
                            f"{type(value).__name__}, not a string literal"
                        )

    assert not offenders, offenders


def test_no_fstring_or_concatenation_inside_the_template_functions():
    tree = ast.parse(RETRIEVER.read_text(encoding="utf-8"))

    offenders = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in {"sql_retrieve", "graph_retrieve"}):
            continue

        for stmt in ast.walk(node):
            if isinstance(stmt, ast.JoinedStr):
                offenders.append(f"f-string in {node.name}")

            if isinstance(stmt, ast.BinOp) and isinstance(stmt.op, ast.Mod):
                offenders.append(f"%-format in {node.name}")

            if isinstance(stmt, ast.BinOp) and isinstance(stmt.op, ast.Add):
                if any(
                    isinstance(side, ast.Constant) and isinstance(side.value, str)
                    for side in (stmt.left, stmt.right)
                ):
                    offenders.append(f"string concatenation in {node.name}")

    assert not offenders, offenders


# --------------------------------------------------------------------------------
# The control question and the revenue question, end to end
# --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gemini(project_root):
    load_dotenv(project_root / ".env")

    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY is not set")


def plan(query: str) -> dict:
    from gemini_query_planner import plan_query_with_gemini

    return plan_query_with_gemini(query)


@pytest.mark.requires_gemini
@pytest.mark.requires_stack
def test_revenue_question_produces_a_representable_sql_only_plan(gemini):
    """The question that crashed the workflow in the frozen baseline.

    Baseline: ValueError: Invalid graph intent from Gemini: None.
    """
    result = plan(REVENUE)

    assert result["answerable"] is True
    assert result["sql_intent"] == "seller_performance"
    assert result["graph_intent"] is None, "a revenue question needs no graph traversal"
    assert result["sql_plan"]["sort_by"] == "total_item_revenue", (
        "a revenue question must rank by revenue, not by the template's default"
    )


@pytest.mark.requires_gemini
@pytest.mark.requires_stack
def test_control_question_refuses_or_returns_different_evidence(gemini):
    """The M2 exit criterion for the control, stated as the criterion states it.

    Preferred outcome is a refusal. A routed-but-different-evidence outcome also
    satisfies the criterion. Byte-identical evidence to the flagship question does
    not, and is the F-03 behaviour.
    """
    control_plan = plan(CONTROL)

    if not control_plan["answerable"]:
        assert control_plan["refusal_reason"], "a refusal must say why"
        assert control_plan["sql_intent"] is None
        assert control_plan["graph_intent"] is None
        assert control_plan["vector_artifact_groups"] == []
        return

    # Routed anyway: then its evidence must differ from the flagship question's.
    flagship_plan = plan(FLAGSHIP)

    control_signature = json.dumps(
        {
            "sql": control_plan.get("sql_plan"),
            "graph": control_plan.get("graph_plan"),
            "vector": control_plan.get("vector_artifact_groups"),
        },
        sort_keys=True,
        default=str,
    )
    flagship_signature = json.dumps(
        {
            "sql": flagship_plan.get("sql_plan"),
            "graph": flagship_plan.get("graph_plan"),
            "vector": flagship_plan.get("vector_artifact_groups"),
        },
        sort_keys=True,
        default=str,
    )

    assert hashlib.sha256(control_signature.encode()).hexdigest() != hashlib.sha256(
        flagship_signature.encode()
    ).hexdigest(), (
        "the control question was routed with the same plan as the flagship question"
    )


@pytest.mark.requires_gemini
@pytest.mark.requires_stack
def test_a_refusal_is_not_the_default_behaviour(gemini):
    """A refusal only means something if real questions are not refused."""
    result = plan(FLAGSHIP)

    assert result["answerable"] is True
    assert result["refusal_reason"] is None
    assert result["sql_intent"] or result["graph_intent"] or result["vector_artifact_groups"]


# --------------------------------------------------------------------------------
# Routing decision, without Gemini
# --------------------------------------------------------------------------------


def test_route_after_planning_sends_an_unanswerable_plan_to_the_refusal_node():
    from agentic_workflow import route_after_planning

    assert (
        route_after_planning(
            {"planned_route": {"answerable": False, "refusal_reason": "nonsense"}}
        )
        == "refusal"
    )


def test_route_after_planning_sends_an_empty_plan_to_the_refusal_node():
    from agentic_workflow import route_after_planning

    assert (
        route_after_planning(
            {
                "planned_route": {
                    "answerable": True,
                    "sql_intent": None,
                    "graph_intent": None,
                    "vector_artifact_groups": [],
                }
            }
        )
        == "refusal"
    )


def test_route_after_planning_sends_a_sql_only_plan_to_retrieval():
    from agentic_workflow import route_after_planning

    assert (
        route_after_planning(
            {
                "planned_route": {
                    "answerable": True,
                    "sql_intent": "seller_performance",
                    "graph_intent": None,
                    "vector_artifact_groups": [],
                }
            }
        )
        == "retrieve"
    )
