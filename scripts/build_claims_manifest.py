"""Build audit/claims.json: the 110 audited claims, each bound to a check (M9).

The first audit's verdicts live in AUDIT.md as prose in a table. This turns them into
a manifest a program can re-run, so "zero contradicted claims" is a command rather than
a reading. The baseline verdict is carried alongside the new one, because the
before/after is the point.

    python scripts/build_claims_manifest.py
    python scripts/audit_claims.py
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "audit" / "baseline_claims.json"
OUTPUT = PROJECT_ROOT / "audit" / "claims.json"

RETRIEVER = "src/retrieval/hybrid_retriever.py"
PLANNER = "src/planning/gemini_query_planner.py"
WORKFLOW = "src/orchestration/agentic_workflow.py"
API = "src/api/main.py"
UI = "src/ui/streamlit_app.py"

# claim id -> {check: name, **kwargs}. Anything absent falls back to `manual` with
# the baseline verdict and is counted separately in the report, so the coverage
# figure cannot be quietly inflated.
CHECKS: dict[int, dict] = {
    # ---------------------------------------------------------------- PostgreSQL
    1: {"check": "sql_count", "query": "SELECT count(*) FROM ecommerce.customers", "expected": 99441},
    2: {"check": "sql_count", "query": "SELECT count(*) FROM ecommerce.sellers", "expected": 3095},
    3: {"check": "sql_count", "query": "SELECT count(*) FROM ecommerce.products", "expected": 32340},
    4: {"check": "sql_count", "query": "SELECT count(*) FROM ecommerce.orders", "expected": 99441},
    5: {"check": "sql_count", "query": "SELECT count(*) FROM ecommerce.order_items", "expected": 111046},
    6: {"check": "sql_count", "query": "SELECT count(*) FROM ecommerce.payments", "expected": 103886},
    7: {"check": "sql_count", "query": "SELECT count(*) FROM ecommerce.reviews", "expected": 99224},
    8: {"check": "sql_count",
        "query": "SELECT count(*) FROM ecommerce.product_category_translations "
                 "WHERE product_category_name_english IS NOT NULL",
        "expected": 73,
        "note": "The first audit found this NULL in 73/73 (F-04) and gave the claim a "
                "CAVEAT. Now populated 73/73, so the caveat is retired."},
    9: {"check": "file_exists", "paths": ["database/postgres/schema.sql", "database/postgres/indexes.sql"]},
    10: {"check": "sql_count",
         "query": "SELECT count(*) FROM pg_views WHERE schemaname = 'ecommerce'",
         "expected": 6},
    11: {"check": "file_exists", "paths": ["src/database/postgres_validator.py"]},

    # ---------------------------------------------------------------- Neo4j
    12: {"check": "cypher_at_least", "query": "CALL db.labels() YIELD label RETURN count(label)", "minimum": 9},
    13: {"check": "cypher_at_least",
         "query": "CALL db.relationshipTypes() YIELD relationshipType RETURN count(relationshipType)",
         "minimum": 8},
    14: {"check": "cypher_count",
         "query": "MATCH ()-[r:PLACED]->() RETURN count(r)", "expected": 99441},
    15: {"check": "cypher_at_least",
         "query": "SHOW CONSTRAINTS YIELD name RETURN count(name)", "minimum": 1},
    16: {"check": "file_exists", "paths": ["src/graph/neo4j_validator.py"]},
    17: {"check": "cypher_at_least",
         "query": "MATCH (c:Customer)-[:PLACED]->(o:Order)-[:HAS_PAYMENT]->(p:Payment) RETURN count(*)",
         "minimum": 1},
    18: {"check": "cypher_at_least",
         "query": "MATCH (c:Customer)-[:PLACED]->(o:Order)-[:HAS_REVIEW]->(r:Review) RETURN count(*)",
         "minimum": 1},
    19: {"check": "cypher_at_least",
         "query": "MATCH (r:Review)<-[:HAS_REVIEW]-(o:Order)-[:CONTAINS_ITEM]->(i:OrderItem)"
                  "-[:REFERENCES_PRODUCT]->(p:Product) "
                  "MATCH (i)-[:SOLD_BY]->(s:Seller) "
                  "WHERE r.review_score <= 2 RETURN count(*)",
         "minimum": 1,
         "note": "SOLD_BY hangs off OrderItem, not Product -- an order item is what a "
                 "seller sells. The first version of this check walked "
                 "Product-[:SOLD_BY]->Seller, matched 0, and reported the claim "
                 "CONTRADICTED. The path is real: 17,756 traversals from reviews "
                 "scoring <= 2."},
    20: {"check": "cypher_count",
         "query": "MATCH (o:Order) WHERE NOT (o)<-[:PLACED]-() RETURN count(o)", "expected": 0},

    # ---------------------------------------------------------------- Synthetic corpus
    21: {"check": "qdrant_group_has_entity_ids", "group": "support_tickets", "keys": ["seller_id"]},
    27: {"check": "qdrant_composition", "expected": {"support_tickets": 3000, "customer_emails": 985,
                       "logistics_incidents": 1000, "warranty_claims": 1000,
                       "policy_documents": 40, "troubleshooting_guides": 73},
         "retired_total": 8152},

    # ------------------------------------------------- the entity-linkage failures
    28: {"check": "qdrant_entity_ids_present", "min_fraction": 0.5},
    29: {"check": "qdrant_entity_ids_present", "min_fraction": 0.5},
    30: {"check": "qdrant_group_has_entity_ids", "group": "warranty_claims",
         "keys": ["product_id", "seller_id"]},
    34: {"check": "qdrant_entity_ids_present", "min_fraction": 0.5},

    31: {"check": "source_contains", "path": RETRIEVER, "patterns": [r"all-MiniLM-L6-v2"]},
    32: {"check": "qdrant_vector_dim", "expected": 384},
    33: {"check": "qdrant_composition", "expected": {"support_tickets": 3000, "customer_emails": 985,
                       "logistics_incidents": 1000, "warranty_claims": 1000,
                       "policy_documents": 40, "troubleshooting_guides": 73},
         "retired_total": 8152},
    35: {"check": "source_contains", "path": RETRIEVER, "patterns": [r"artifact_groups"]},

    # ---------------------------------------------------------------- Routing
    38: {"check": "legs_can_be_skipped"},
    39: {"check": "pytest_passes", "tests": ["tests/test_allowlist_frozen.py"]},
    40: {"check": "pytest_passes",
         "tests": ["tests/test_workflow_routing.py::test_templates_are_static_literals_not_assembled_strings"]},
    42: {"check": "legs_can_be_skipped"},
    49: {"check": "sql_only_plan_is_representable"},
    50: {"check": "source_contains", "path": PLANNER,
         "patterns": [r"def validate_and_normalize_plan", r"def resolve_plan_parameters"]},
    51: {"check": "pytest_passes", "tests": ["tests/test_plan_parameters.py"]},

    # ---------------------------------------------------------------- Workflow
    43: {"check": "source_contains", "path": WORKFLOW,
         "patterns": [r"def query_planner_node", r"def answer_generation_node",
                      r"def answer_evaluation_node"]},
    44: {"check": "source_contains", "path": WORKFLOW, "patterns": [r"langgraph|StateGraph"]},
    45: {"check": "source_contains", "path": WORKFLOW, "patterns": [r"evaluat"]},
    52: {"check": "pytest_passes", "tests": ["tests/test_context_builder_grouping.py"]},

    # ---------------------------------------------------------------- Evaluation
    58: {"check": "deterministic_checks_can_fail_a_run"},
    59: {"check": "feature_now_exists", "paths": [WORKFLOW],
         "patterns": [r"def replan_node", r"should_replan"], "milestone": "M4"},

    # ---------------------------------------------------------------- API
    60: {"check": "source_contains", "path": API,
         "patterns": [r'@app\.get\("/health"\)', r'@app\.post\("/query"']},
    61: {"check": "health_reports_real_dependencies"},
    62: {"check": "source_contains", "path": API, "patterns": [r"class QueryRequest", r"class QueryResponse"]},
    63: {"check": "source_contains", "path": API,
         "patterns": [r"source_summary", r"evaluation_summary", r"output_files"]},
    64: {"check": "manual", "verdict": "NOT_CLAIMED",
         "evidence": "The current documentation makes no claim about the 2026-07-12 "
                     "api_validation_report.json. The live API is verified by "
                     "tests/test_api_security.py and tests/test_api_query_contract.py, "
                     "and by the M8 deployment checks."},
    65: {"check": "pytest_passes", "tests": ["tests/test_ui_evidence_trail.py"]},
    66: {"check": "source_absent", "path": UI,
         "patterns": [r"import psycopg2", r"from sqlalchemy", r"import neo4j", r"qdrant_client"]},

    # ---------------------------------------------------------------- Observability
    67: {"check": "source_contains", "path": "src/observability/observability.py",
         "patterns": [r"workflow_events\.jsonl"]},
    70: {"check": "feature_now_exists", "paths": ["src/observability/otel.py"],
         "patterns": [r"input_tokens", r"cost_usd"], "milestone": "M5/M7"},

    # ---------------------------------------------------------------- Docker
    71: {"check": "file_exists", "paths": ["docker-compose.yml", "Dockerfile.api", "Dockerfile.ui"]},
    72: {"check": "source_contains", "path": "docker-compose.yml",
         "patterns": [r"POSTGRES_HOST: postgres", r"NEO4J_URI: bolt://neo4j"]},

    # ---------------------------------------------------------------- Security
    78: {"check": "pytest_passes", "tests": ["tests/test_allowlist_frozen.py"]},
    79: {"check": "pytest_passes", "tests": ["tests/test_allowlist_frozen.py"]},
    80: {"check": "pytest_passes",
         "tests": ["tests/test_workflow_routing.py::test_templates_are_static_literals_not_assembled_strings"]},
    81: {"check": "pytest_passes", "tests": ["tests/test_allowlist_frozen.py"]},
    82: {"check": "runtime_is_read_only"},
    85: {"check": "feature_now_exists", "paths": [API],
         "patterns": [r"require_bearer_token", r"HTTPBearer"], "milestone": "M6"},
    88: {"check": "manual", "verdict": "CAVEAT",
         "evidence": "M6 closed the unauthenticated-insertion path (Qdrant now requires a "
                     "key) and the planner refuses injected instructions (E74, live). "
                     "Whether the answer agent obeys a poisoned document that reached the "
                     "corpus by another route is UNPROVEN -- E83's payload never ranked "
                     "high enough to be retrieved. docs/M6_FINDINGS.md states this."},

    # ---------------------------------------------------------- negative claims
    93: {"check": "feature_now_exists", "paths": ["tests/eval/eval_set_v1.json"],
         "patterns": [r"expected_refusal_terms", r"items"], "milestone": "M3"},
    94: {"check": "feature_now_exists", "paths": ["docs/M3_RESULTS.md"],
         "patterns": [r"single-agent baseline|baseline"], "milestone": "M3"},
    95: {"check": "feature_now_exists", "paths": [WORKFLOW],
         "patterns": [r"def replan_node", r"should_replan"], "milestone": "M4"},
    97: {"check": "feature_now_exists", "paths": ["src/observability/query_cache.py"],
         "patterns": [r"class QueryCache", r"class InMemoryLRUCache"], "milestone": "M5"},
    98: {"check": "feature_now_exists", "paths": ["src/observability/resilience.py"],
         "patterns": [r"circuit|breaker", r"backoff"], "milestone": "M4"},
    99: {"check": "feature_now_exists", "paths": ["tests/eval/results/m5_cache_concurrency.json"],
         "patterns": [r"concurren"], "milestone": "M5"},
    100: {"check": "feature_now_exists", "paths": ["src/observability/otel.py"],
          "patterns": [r"cost_usd", r"INPUT_USD_PER_TOKEN"], "milestone": "M5/M7"},
    102: {"check": "feature_now_exists", "paths": ["render.yaml"],
          "patterns": [r"enterprise-ai-demo-api"], "milestone": "M8"},

    # ---------------------------------------------------------------- Dataset
    107: {"check": "sql_at_least", "query": "SELECT count(*) FROM ecommerce.orders", "minimum": 1},
    108: {"check": "sql_count",
          "query": "SELECT count(*) FROM information_schema.tables WHERE table_schema='ecommerce'"
                   " AND table_type='BASE TABLE'",
          "expected": 8},

    # ---------------------------------------------------------------- Judgement
    104: {"check": "test_suite_passes", "minimum": 299},
    105: {"check": "manual", "verdict": "UNVERIFIABLE",
          "evidence": "A claim about who performed the work. No command settles it; "
                      "the git history is the only evidence and it is self-reported."},
    106: {"check": "manual", "verdict": "UNVERIFIABLE",
          "evidence": "As #105 -- a claim about process, not about the system."},
    109: {"check": "manual", "verdict": "NOT_CLAIMED",
          "evidence": "geolocation_cleaned.csv is on disk and loaded into neither store. "
                      "The documentation does not claim otherwise."},
}


def main() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    claims = []

    for row in baseline:
        spec = CHECKS.get(row["id"])

        if spec is None:
            spec = {
                "check": "manual",
                "verdict": row["baseline"],
                "evidence": "Inherited from the 2026-08-20 audit; no executable check "
                            "written. Counted as unchecked in the coverage figure.",
                "inherited": True,
            }

        claims.append({
            "id": row["id"],
            "claim": row["claim"],
            "baseline_verdict": row["baseline"],
            "baseline_evidence": row["evidence"],
            **spec,
        })

    executable = sum(1 for c in claims if c["check"] != "manual")
    manual_judged = sum(1 for c in claims if c["check"] == "manual" and not c.get("inherited"))
    inherited = sum(1 for c in claims if c.get("inherited"))

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps({
        "schema_version": 1,
        "baseline_audit": {"date": "2026-08-20", "commit": "f8dc45c",
                           "source": "AUDIT.md", "total": len(claims)},
        "coverage": {"executable": executable, "manual_judged": manual_judged,
                     "inherited_unchecked": inherited},
        "claims": claims,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"{len(claims)} claims -> {OUTPUT}")
    print(f"  executable checks : {executable}")
    print(f"  manual judgements : {manual_judged}")
    print(f"  inherited/unchecked: {inherited}")


if __name__ == "__main__":
    main()
