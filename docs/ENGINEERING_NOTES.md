# Engineering notes — what the audit found, and what changed

The August 2026 audit (`AUDIT.md`) tested 110 claims and raised 20 numbered findings
across four themed investigations. This is the disposition of every one of them,
measured against the finished system rather than remembered.

**Status counts:** 14 fixed, 3 partially fixed with the remainder stated, 3 open and
documented.

Each row's "after" is reproducible. `python scripts/audit_claims.py` re-checks the
claims; the named tests are in the suite.

---

## The four investigations

### P1 — Is the synthetic layer statistically linked, or ID-stamped?

**Found:** neither. A deterministic `ORDER BY … LIMIT 3000` over PostgreSQL columns
produced a degenerate slice — not random ID-stamping, not independent ground truth.

**Now:** the corpus was rebuilt in M1 around entity linkage, and deliberately shrank
from 8,152 records to **6,098** as low-signal documents were dropped
(`docs/CORPUS_DESIGN.md`). The selection bias is documented rather than removed: the
synthetic layer is derived from the Olist data it annotates, so it cannot corroborate
it. That limitation is in the README.

### P2 — Is the tool allowlist real?

**Found:** the allowlist held against four of five attacks. The fifth was not an
allowlist problem: the *runtime* was a PostgreSQL superuser running committing
read-write transactions, with write-capable Neo4j sessions and an unauthenticated
Qdrant. "Read-only" was enforced by nothing below the application — the only thing
preventing a write was that nobody had written a DML template.

**Now:** the query path connects as a non-superuser SELECT-only role, inside
`SET TRANSACTION READ ONLY`, with Neo4j sessions in read mode, and Qdrant requires an
API key. **The audit's own write probes are now tests**, and each asserts a
*permission denial* rather than merely that nothing happened — a write that silently
no-ops is indistinguishable from one that committed and rolled back, and only one of
those is a control. `tests/test_postgres_readonly_role.py`.

The narrow claim the audit said should be made is the one the README makes: *the tool
allowlist prevents arbitrary queries.* Not "the runtime is read-only" as a slogan.

### P3 — Who decided the five cases passed?

**Found:** the five "passing" validation cases were **the planner's own few-shot
examples, verbatim**. The system was being tested on its answer key. The deterministic
checks alongside the LLM evaluator were keyword-presence and length; four of five were
`severity: medium` and could not cause a FAIL, leaving `len >= 1000` as the only
decisive gate.

**Now:** an 82-item held-out evaluation set, built under a rule that **no question may
appear in the planner's few-shot prompt, in the five original cases, or in their
paraphrases**. Routing accuracy 50/53 = 94.3% [84.6, 98.1] against a single-agent
baseline of 43/53 = 81.1% [68.6, 89.4]. The judge was calibrated against a blind human
rater: **kappa 0.390 [0.000, 0.788], below the 0.7 floor**, reported as a failure rather
than quietly dropped. `docs/M3_RESULTS.md`.

### P4 — Reproducibility

**Found:** the generator was seeded but not deterministic; a clean clone could not
reproduce the system because the data-cleaning step was not in the repository; CI did
not exist.

**Now:** dependencies are pinned (F-11), CI exists and runs on every branch, and
`docs/SETUP.md` documents the real path. **Clean-clone reproduction is still not
possible** — the raw Olist data is not in the repository and cannot be — and that is
stated rather than implied.

---

## The twenty findings

| | Finding | Status | Evidence |
|---|---|---|---|
| **F-01** | CRITICAL: every entity ID dropped at Qdrant ingest | **fixed** | 0 of 8,152 points carried an Olist ID. Now the majority do, and an ID taken from a Qdrant payload resolves to a PostgreSQL row — `audit/checks.py::entity_id_joins_to_postgres`, `tests/test_entity_id_join.py` |
| **F-02** | CRITICAL: `text_preview` read but never written — no document text reached the LLM | **fixed** | M2. Caught by a compaction test; two payload-level tests passed on the broken code, which is written up in `docs/M2_CHANGES.md` |
| **F-03** | CRITICAL: SQL and graph retrieval invariant to the question | **fixed** | Parameterised templates with planner-resolved filters. `tests/test_plan_parameters.py`, `tests/test_dropped_filters.py` |
| **F-04** | `product_category_name_english` NULL in 73/73 | **fixed** | Now populated **73/73**. Claim #8 moved CAVEAT → CONFIRMED |
| **F-05** | `sentiment_label` NULL for all 99,224 reviews | **fixed** | **0 NULL of 99,224** |
| **F-06** | `standardize_columns` silently invents missing columns | **fixed** | The function no longer exists in `src/data_engineering/` |
| **F-07** | `/health` a static literal, green while everything was down | **fixed** | Three parallel probes, 503 when any fails. `tests/test_api_security.py` |
| **F-08** | Unauthenticated exception-detail disclosure | **fixed** | Bearer auth on `/query`; 500s return an opaque `incident_id`, detail to the server log |
| **F-09** | Embedding model reloaded from disk on every query | **fixed** | Loaded once per process: **−1,146 ms per request** (2,168.5 → 1,022.5 ms median) |
| **F-10** | 9.4% of documents truncated at embedding time | **open** | 763 of 8,152 at the time of the audit. Deferred deliberately; stated in the README |
| **F-11** | `requirements.txt` had zero version pins | **fixed** | 23 pinned, **0 unpinned**. CI installs them on a clean runner, which is the check |
| **F-12** | The project's own validators could not detect F-01 or F-02 | **fixed** | 309 tests, including seam tests that assert an ID from one store resolves in another |
| **F-13** | Dead code | **fixed** | Removed in M4 |
| **F-14** | The API validator does not reproduce today | **not claimed** | The 2026-07-12 report is no longer cited. Replaced by `tests/test_api_security.py`, `tests/test_api_query_contract.py` and the live M8 deployment checks. Claim #64 is `NOT_CLAIMED` with that reason recorded |
| **F-15** | 73 of 79 policy documents were the same sentence | **partially fixed** | The corpus rebuild cut policy documents to **40**. The template repetition is reduced, not eliminated |
| **F-16** | 100% of ticket `customer_message` fields were raw Portuguese Olist review text | **partially fixed** | Addressed in the M1 corpus rebuild; the synthetic layer remains derived from Olist text and cannot corroborate it |
| **F-17** | Real credentials as source-code defaults in 17 places | **partially fixed** | Every **password** default removed — an unset variable now fails loudly. **8 username-class defaults remain**, so with no environment at all the query path would connect as the schema owner. Asserted by `test_without_the_readonly_role_the_runtime_falls_back_to_the_owner` |
| **F-18** | Hardcoded `"overall_status": "PASS"` in generated reports | **fixed in M9** | Still open when M9 re-audited. See below |
| **F-19** | Partial failures could escape the observability trace | **fixed** | OpenTelemetry. A crash *between* spans marks the root ERROR — `test_a_crash_between_spans_still_fails_the_root_span` |
| **F-20** | Minor: duplicated f-string; `ORDER BY severity` on a string column | **fixed** | The severity sort is a static `CASE` expression (M2 decision D-3) |

---

## F-18, and why it survived seven milestones

The M9 re-audit found F-18 **still open**, and M4 had made half of it worse.

`hybrid_retriever.py` wrote `"overall_status": "PASS"` as a literal into its report
summary — in the same dictionary that computes `unavailable_legs` three fields below.
Once M4 added graceful degradation, a retrieval with PostgreSQL down produced a report
that said `PASS` *and* named a dead dependency. The report contradicted itself, and the
literal is the half a reader trusts.

`agentic_workflow.py` did `evaluation_summary.get("overall_status", "PASS")`. A run
whose evaluator produced no verdict reported success. **The pipeline failed open.**

Both are now derived — `"DEGRADED" if unavailable_legs else "PASS"`, and
`.get("overall_status") or "UNKNOWN"` — and guarded by
`tests/test_status_is_measured_not_asserted.py`, which parses the source rather than
grepping it, so the comment explaining the fix cannot trip the test that protects it.

**This is the same defect as the M7 tracer bug**: a status initialised to success and
downgraded only when something remembers to. M7 fixed it in the trace. It was still
in the reports. Worth naming as a pattern, because it is the project's characteristic
failure mode — *asserting* an outcome in the same breath as computing the evidence
that contradicts it.

---

## Findings the rebuild produced on its own

Not from the audit. Each was found by measuring something that had not been measured.

| Milestone | Finding |
|---|---|
| M2 | Two payload-level tests passed on code broken by F-02; only the compaction test caught it. Test placement, not test count |
| M3 | 26 of 82 items crashed on a rate limit, voiding the first full run. Re-ran all 82 rather than merging two partial runs |
| M5 | The M3 cost figure was wrong by **2.8×** — $0.105 quoted against an *assumed* price. Corrected to **$0.2972** at the published rate |
| M5 | The evaluator is the most expensive agent: **41% of total spend** |
| M6 | Three refused writes opened the Postgres circuit breaker for 60 s — a security control acting as a denial-of-service primitive. Non-transient errors now record success |
| M7 | Retrieval spans reported `duration_ms = 0.0`; they were reconstructed after the fact rather than bracketing the work |
| M7 | `graph_intent` reproduces on only 10 of 14 replays; the two that diverge now route *better* than the recording |
| M8 | **Every refused question returned HTTP 500** through the API — 32 of 82 eval items — because `refusal_node` returns no `answer_provider` and the response model required it |
| M8 | The Streamlit UI never sent the bearer token M6 added, so every query from the page would have 401'd |
| M8 | The UI's question picker was a silent no-op: 14 UI tests drove the page directly and none exercised the path a user takes |
| M9 | **CI never ran on M2–M8.** It triggered on `main` and pull requests; every milestone was committed to its own branch with no PR. The gate guarded nothing for seven milestones |
| M9 | The test suite is not deterministic: **1 failure in 9 full runs**, always the live-Gemini planner test. The audit gates on the 312 deterministic tests |
| M9 | **F-18 was still open**, and M4 had made half of it worse — a retrieval report saying `PASS` beside the dependency that failed |
| M9 | The README's test count went stale *during M9* — 300 against a suite that had grown to 316. `test_suite_passes` asserts a floor, which is the right gate for green and the wrong check for a published number. Claim #127 now compares against what pytest collects |

---

## What the audit harness itself got wrong

The M9 audit is code, and code has bugs. Recording them because an audit that reports
only other people's mistakes is not trustworthy.

- It reported the corpus **2,054 documents short** and nearly published that as a
  silent 25% regression. `docs/CORPUS_DESIGN.md` had retired the 8,152 figure
  deliberately in M1. **The manifest was checking the number from the document under
  audit instead of from the current design** — exactly the mistake the audit exists to
  catch, committed by the audit. The check is now per artifact group, so drift in one
  group cannot hide inside a plausible total.
- It reported the live demo API contradicted because the health body contained
  `"mode":"demo"` and the check looked for `"mode": "demo"` — a missing space.
- Six checks failed on wrong table names, wrong relationship directions
  (`SOLD_BY` hangs off `OrderItem`, not `Product`) and plural payload keys.

Of 127 claims, **87 have executable checks, 5 are manual judgements, and 35 are
inherited** from the 2026-08-20 audit without a new check. The manifest flags those 35
`inherited: true` and the build script prints the count, so the coverage figure cannot
be quietly inflated.
