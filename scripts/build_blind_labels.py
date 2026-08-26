"""Build the blind judge-calibration file.

Produces tests/eval/calibration/blind_labels.md -- question, answer, evidence, and
NOTHING that reveals the evaluator's score, the item id, the category, or the eval-set
order. The mapping from blind number back to item is written to blind_key.json, which
the labeller must not read.

Shuffled with a fixed seed so the file is reproducible without being ordered.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS = PROJECT_ROOT / "tests" / "eval" / "results"
OUT_DIR = PROJECT_ROOT / "tests" / "eval" / "calibration"

FULL_RUN = RESULTS / "run_20260825T145424Z_full2.json"
SHUFFLE_SEED = 20260826


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def evidence_digest(result: dict, limit: int = 6) -> list[str]:
    """A compact view of what the answer had to work with.

    Grounding cannot be judged from record counts alone -- the counts say how much
    evidence there was, not what it said. Identifiers and a few salient values are
    included so a human can check whether a claim is supported, without reproducing
    whole rows.
    """
    lines: list[str] = []
    evidence = result.get("evidence") or {}

    def summarise(rows, label, keys):
        if not rows:
            return
        lines.append(f"  {label}:")
        for row in rows[:limit]:
            parts = []
            for key in keys:
                value = row.get(key)
                if value not in (None, "", []):
                    text = str(value)
                    parts.append(f"{key}={text[:38]}")
            if parts:
                lines.append("    - " + "; ".join(parts[:6]))

    summarise(
        evidence.get("sql_record_sample"), "SQL",
        ["seller_id", "order_id", "product_id", "customer_unique_id", "review_id",
         "total_orders", "total_item_revenue", "avg_review_score",
         "late_delivery_rate", "late_delivery_orders", "order_status",
         "delivery_delay_days", "total_payment_value", "review_score",
         "product_category_name_english", "total_items_sold", "seller_state"],
    )
    summarise(
        evidence.get("graph_record_sample"), "GRAPH",
        ["seller_id", "ticket_id", "claim_id", "incident_id", "policy_document_id",
         "guide_id", "severity", "issue_type", "claim_status", "category",
         "order_id", "product_id", "delivery_delay_days"],
    )

    vector = evidence.get("vector_record_sample") or []
    if vector:
        lines.append("  DOCUMENTS:")
        for row in vector[:limit]:
            ident = row.get("artifact_id") or "?"
            method = row.get("retrieval_method") or "semantic"
            preview = str(row.get("text_preview") or "").replace("\n", " ")[:150]
            lines.append(f"    - {ident} ({method}): {preview}")

    return lines or ["  (no evidence records captured)"]


def build_entries() -> list[dict]:
    full = load(FULL_RUN)
    entries: list[dict] = []

    for result in full["results"]:
        # Only items the judge actually scored can enter a kappa calculation.
        if result["evaluator"]["grounding_score"] is None:
            continue
        entries.append({
            "item_id": result["id"],
            "run": "full2",
            "question": result["question"],
            "answer_text": result["answer_text"],
            "evidence_counts": {
                "sql": result["evidence"]["sql_records"],
                "graph": result["evidence"]["graph_records"],
                "vector": result["evidence"]["vector_records"],
            },
            "evidence_digest": evidence_digest(result),
            "evaluator_grounding": result["evaluator"]["grounding_score"],
        })

    # The post-fix run: several answers changed substantively, and C43 must appear
    # as two separate entries so the labeller scores both states blind.
    fix_runs = sorted(RESULTS.glob("*m3fix.json"))

    if fix_runs:
        fix = load(fix_runs[-1])
        for result in fix["results"]:
            if result["evaluator"]["grounding_score"] is None:
                continue
            entries.append({
                "item_id": result["id"],
                "run": "m3fix",
                "question": result["question"],
                "answer_text": result["answer_text"],
                "evidence_counts": {
                    "sql": result["evidence"]["sql_records"],
                    "graph": result["evidence"]["graph_records"],
                    "vector": result["evidence"]["vector_records"],
                },
                "evidence_digest": evidence_digest(result),
                "evaluator_grounding": result["evaluator"]["grounding_score"],
            })

    return entries


RUBRIC = """\
## Rubric

Score **grounding** 1-5 for each answer. This is the same dimension the automated
evaluator scores, and the instruction it is given is:

> Evaluate whether the generated answer is grounded in the provided retrieval context.
> Do not judge using outside knowledge. Use only the retrieval context and the answer.
> - The answer should use SQL evidence for structured facts.
> - The answer should use graph evidence for connected entity relationships.
> - The answer should use document evidence for retrieved tickets, policies, guides,
>   incidents, emails or warranty claims.
> - The answer should not invent unsupported facts.
> - The answer should include limitations if evidence is incomplete.
> - Do not fail the answer only because it is cautious.

Suggested anchors:

| score | meaning |
|---|---|
| 5 | every factual claim traces to the evidence shown; caveats where evidence is thin |
| 4 | grounded, with a minor unsupported flourish or a missing caveat |
| 3 | mostly grounded, but at least one material claim is not supported by the evidence |
| 2 | several claims unsupported, or a headline claim contradicts the evidence |
| 1 | largely or entirely ungrounded |

Judge **grounding only**. Not helpfulness, not completeness, not whether the question
*should* have been refused, and not whether the retrieval was any good -- an answer
that accurately reports thin evidence can still be a 5.

Write your score on the `SCORE:` line for each entry. Leave `_` to skip an item.
"""


def main() -> None:
    entries = build_entries()

    rng = random.Random(SHUFFLE_SEED)
    order = list(range(len(entries)))
    rng.shuffle(order)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    key = []
    lines = [
        "# Blind grounding labels — judge calibration",
        "",
        f"**{len(entries)} entries.** Shuffled; the numbering carries no information "
        "about category, eval-set order, or which run an answer came from.",
        "",
        "The automated evaluator's scores are deliberately absent from this file and "
        "will not be shown until these labels are returned.",
        "",
        "Some questions appear more than once with different answers — the system was "
        "changed between runs. Score each entry on its own merits; they are not "
        "duplicates and are not necessarily the same score.",
        "",
        RUBRIC,
        "",
        "---",
        "",
    ]

    for position, index in enumerate(order, start=1):
        entry = entries[index]
        key.append({
            "blind_number": position,
            "item_id": entry["item_id"],
            "run": entry["run"],
            "evaluator_grounding": entry["evaluator_grounding"],
        })

        counts = entry["evidence_counts"]
        answer = (entry["answer_text"] or "").strip() or "(no answer text)"

        lines += [
            f"## {position}",
            "",
            f"**Question:** {entry['question']}",
            "",
            f"**Evidence records:** SQL {counts['sql']} · graph {counts['graph']} · "
            f"documents {counts['vector']}",
            "",
            "<details><summary>evidence detail</summary>",
            "",
            "```",
            *entry["evidence_digest"],
            "```",
            "",
            "</details>",
            "",
            "**Answer:**",
            "",
            "```",
            answer,
            "```",
            "",
            "SCORE: _",
            "",
            "---",
            "",
        ]

    (OUT_DIR / "blind_labels.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "blind_key.json").write_text(
        json.dumps(
            {
                "purpose": "Maps blind numbers back to items and the evaluator's scores. "
                           "Do not read before hand-labelling.",
                "shuffle_seed": SHUFFLE_SEED,
                "entries": key,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    repeated: dict[str, int] = {}
    for k in key:
        repeated[k["item_id"]] = repeated.get(k["item_id"], 0) + 1

    print(f"entries          : {len(entries)}")
    print(f"items appearing twice: {sorted(i for i, n in repeated.items() if n > 1)}")
    print(f"wrote            : {OUT_DIR / 'blind_labels.md'}")
    print(f"key (do not open): {OUT_DIR / 'blind_key.json'}")


if __name__ == "__main__":
    main()
