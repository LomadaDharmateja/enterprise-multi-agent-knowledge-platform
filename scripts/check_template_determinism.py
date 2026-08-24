"""Does the same retrieval template return the same rows twice?

M0 task 5 turned this up: two baseline questions routed to the same SQL intent and
got different evidence back. Same template, same bound :limit, different rows. This
script runs every SQL and Cypher template N times and reports how many distinct
result sets each produced.

A template that returns a different answer to the identical question is a
reproducibility problem underneath everything else -- an evaluation set cannot be
scored against a retrieval layer that is not stable.

    python scripts/check_template_determinism.py
    python scripts/check_template_determinism.py --runs 10 --json out.json
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETRIEVER = PROJECT_ROOT / "src" / "retrieval" / "hybrid_retriever.py"


def extract_templates(function_name: str, dict_name: str) -> dict[str, str]:
    tree = ast.parse(RETRIEVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == dict_name
                    and isinstance(stmt.value, ast.Dict)
                ):
                    return {
                        key.value: value.value
                        for key, value in zip(stmt.value.keys, stmt.value.values)
                        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
                    }
    raise SystemExit(f"{dict_name} not found in {function_name}()")


def digest(rows: list[Any]) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check retrieval template determinism.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    engine = create_engine(
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'enterprise_user')}:"
        f"{os.getenv('POSTGRES_PASSWORD', 'enterprise_password')}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/"
        f"{os.getenv('POSTGRES_DB', 'enterprise_ai')}"
    )
    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(
            os.getenv("NEO4J_USERNAME", "neo4j"),
            os.getenv("NEO4J_PASSWORD", "enterprise_neo4j_password"),
        ),
    )

    results: dict[str, Any] = {"runs": args.runs, "limit": args.limit, "sql": {}, "graph": {}}
    unstable = 0

    print(f"running each template {args.runs} times with limit={args.limit}\n")

    print("SQL templates (ecommerce views)")
    for intent, sql in extract_templates("sql_retrieve", "sql_templates").items():
        seen = set()
        for _ in range(args.runs):
            with engine.connect() as connection:
                rows = [
                    dict(r) for r in connection.execute(text(sql), {"limit": args.limit}).mappings()
                ]
            seen.add(digest(rows))
        stable = len(seen) == 1
        unstable += not stable
        results["sql"][intent] = {"distinct_result_sets": len(seen), "stable": stable}
        print(f"  {intent:<26} distinct result sets: {len(seen)}  {'STABLE' if stable else 'UNSTABLE'}")

    print("\nCypher templates (Neo4j)")
    for intent, cypher in extract_templates("graph_retrieve", "cypher_templates").items():
        seen = set()
        for _ in range(args.runs):
            with driver.session() as session:
                rows = [r.data() for r in session.run(cypher, {"limit": args.limit})]
            seen.add(digest(rows))
        stable = len(seen) == 1
        unstable += not stable
        results["graph"][intent] = {"distinct_result_sets": len(seen), "stable": stable}
        print(f"  {intent:<40} distinct result sets: {len(seen)}  {'STABLE' if stable else 'UNSTABLE'}")

    driver.close()

    print(f"\n{unstable} unstable template(s) out of "
          f"{len(results['sql']) + len(results['graph'])}")
    results["unstable_count"] = unstable

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"json written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
