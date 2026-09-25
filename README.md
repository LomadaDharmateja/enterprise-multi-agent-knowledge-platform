# Enterprise Multi-Agent Knowledge Intelligence Platform

A question in English is routed by an LLM planner across three stores — PostgreSQL for
facts, Neo4j for relationships, Qdrant for documents — answered only from what those
stores returned, and scored by a separate evaluator. Every claim in the answer is shown
with the evidence behind it.

**Live demo:** https://enterprise-ai-demo-ui.onrender.com
(API: https://enterprise-ai-demo-api-77mb.onrender.com)
It replays eight recorded runs and calls no model and no database, so it needs no API
key and costs nothing to leave running. See [Deployment](#deployment).

This document is a design doc, not a brochure. It says what the system is, what it
does, **what was measured**, and **what it cannot do**. Every number below is
reproducible from a command in this repository, and the numbered claims — `#41`,
`#82` — are entries in `audit/claims.json`, re-checkable with:

```bash
python scripts/audit_claims.py      # exits non-zero if any claim is contradicted
```

## Why this document is shaped this way

An independent audit in August 2026 tested 110 claims made by an earlier write-up of
this project and found **11 of them contradicted by the running system** (`AUDIT.md`).
Not small ones: the vector store held no entity IDs, so "retrieved documents link back
to SQL and graph evidence" was false; SQL and graph retrieval returned the same rows
regardless of the question; `/health` was a hardcoded `ok` that stayed green while every
dependency was down; and the "read-only runtime" ran as a PostgreSQL superuser through
committing read-write transactions.

The response was a ten-milestone rebuild (`REBUILD_PLAN.md`) under one rule: **every
exit criterion is a number or a passing test.** The audit was then re-run against the
finished system, mechanically.

| | 2026-08-20 | 2026-09-14 |
|---|---:|---:|
| CONFIRMED | 78 | 82 |
| CONFIRMED WITH CAVEAT | 18 | 13 |
| **CONTRADICTED** | **11** | **0** |
| SUPERSEDED — true then, fixed since | — | 11 |
| NOT_CLAIMED — dropped, with a reason | — | 2 |
| UNVERIFIABLE | 3 | 2 |

---

## What it is

```
question
   │
   ▼
planner agent ──────────── picks an intent per store, or refuses
   │                       (Gemini; output validated against a frozen allowlist)
   ▼
retrieval ─── PostgreSQL ─ one of N parameterised SQL templates
          ├── Neo4j ────── one of N parameterised Cypher templates
          └── Qdrant ───── semantic search, filtered to artifact groups
   │
   ▼
refusal gate ───────────── stops here if the evidence does not answer the question
   │
   ▼
answer agent ───────────── writes only from the retrieved evidence
   │
   ▼
evaluator agent ────────── grounding / completeness / business-readiness, 1-5
   │
   ▼
response + OpenTelemetry trace
```

**The LLM never writes a query.** It selects an *intent* — `seller_performance`,
`warranty_product_seller_paths` — and the intent maps to a static, parameterised SQL or
Cypher literal. Filter values are resolved against a vocabulary read from the database;
anything the planner invents is dropped and recorded in `dropped_filters`. No SQL,
Cypher, shell or Python text produced by a model is ever executed `#39 #40 #78-81`.
`tests/test_allowlist_frozen.py` fails if a template stops being a literal.

**Data.** The Olist Brazilian e-commerce dataset — 99,441 customers, 3,095 sellers,
32,340 products, 99,441 orders, 111,046 order items `#1-5` — plus a synthetic corpus of
**6,098 documents** (support tickets, customer emails, logistics incidents, warranty
claims, policies, troubleshooting guides) carrying Olist entity IDs so a retrieved
document joins back to a real row `#27-30`.

---

## What was measured

Everything in this section is a measurement, not an impression. Sources in
`docs/M3_RESULTS.md`, `docs/M5_COST_TABLE.md` and the other milestone findings.

### Retrieval quality — 82 held-out questions

The evaluation set contains **no question** that appears in the planner's few-shot
prompt, in the original five demo cases, or in their paraphrases. That rule exists
because the audit found the original "five passing cases" were the planner's own
few-shot examples, verbatim.

Intervals are **95% Wilson score intervals**, never normal-approximation.

| | multi-agent | single-agent baseline | |
|---|---|---|---|
| **routing accuracy** | **50/53 = 94.3% [84.6, 98.1]** | 43/53 = 81.1% [68.6, 89.4] | **+13.2 pts** |
| SQL intent | 51/53 = 96.2% [87.2, 99.0] | 48/53 = 90.6% [79.8, 95.9] | +5.6 |
| graph intent | 52/53 = 98.1% [90.1, 99.7] | 50/53 = 94.3% [84.6, 98.1] | +3.8 |
| refusal precision | 19/21 = 90.5% [71.1, 97.4] | no refusal mechanism | — |
| refusal recall | 19/29 = 65.5% [47.3, 80.1] | 0/29 = 0.0% [0.0, 11.7] | +65.5 |

The intervals overlap. The honest reading is that the multi-agent system routes better,
and 53 questions is not enough to put a tight bound on how much better.

### Cost and latency

`gemini-3.1-flash-lite` at the published **$0.25/1M input, $1.50/1M output**.

| | mean | p95 |
|---|---:|---:|
| answered query | **$0.00456** | $0.00606 |
| refused query | **$0.00092** | $0.00094 |

Full 82-item run: 779,995 input + 68,153 output tokens = **$0.2972**.

**The evaluator is the most expensive agent — $0.1218, 41% of total spend**, because it
receives the question, the answer and the whole evidence context.

End-to-end latency, 82 items: mean 12,451 ms, p95 22,764 ms. Those include **4.5 s of
deliberate client-side pacing per LLM call** to stay under the free tier's 15 req/min;
net of pacing an answered item is roughly **1,619 ms** of retrieval plus provider time.
A refused query costs a fifth of an answered one — one planner call instead of three.

### Performance work that had a number attached

- The embedding model was being reloaded from disk on **every query**. Loading it once
  per process removed **1,146 ms per request** (median 2,168.5 ms → 1,022.5 ms).
- Query cache: a replayed workload hit **100%**, with one model construction across the
  whole run.
- Concurrency: **5 of 5** parallel queries completed, zero errors, one model
  construction shared across all five.

### Security

Each of these was a live attack in the audit, re-run as a test:

- The runtime connects as a **non-superuser, SELECT-only role**, inside `SET TRANSACTION
  READ ONLY`, with Neo4j sessions in read mode. The audit's own write probes now assert
  a *permission denial*, not merely that nothing happened `#82`.
- Qdrant requires an API key. The audit upserted a poisoned document with no credential.
- `POST /query` and `GET /traces/{run_id}` require a bearer token; 500s return an opaque
  incident id instead of a connection string `#85`.
- `/health` probes all three stores in parallel and returns **503** when any is down
  `#61`.

### Observability

Spans carry cost and tokens as attributes, not as a parallel report — so `/query` and
`/traces/{run_id}` cannot disagree about what a run cost. A crash *between* spans marks
the root span ERROR; the previous tracer initialised every span to `PASS` and a run
could fail with every recorded event saying it succeeded.

**316 tests**, of which **312 are deterministic** and must be green. The other four
make a live Gemini call and are non-deterministic *by construction* — the planner is an
LLM with no pinned temperature, and M7 measured `graph_intent` reproducing on only 10 of
14 replays. Measured over nine consecutive full runs: **1 failure in 9**, always
`test_revenue_question_produces_a_representable_sql_only_plan`.

The audit gates on the 312 deterministic tests. A gate that goes red one run in nine for reasons outside the
code teaches people to re-run it until it passes, which is worse than not having one.

---

## What it cannot do

This section is the reason the rest is credible.

**The evaluator's agreement with a human is poor.** Linear weighted Cohen's kappa
**0.390, 95% CI [0.000, 0.788], n=25** — below the 0.7 floor the milestone set. It was
left unadjusted rather than tuned mid-measurement. Read the 1–5 grounding scores as a
weak signal, not a verdict. The UI says so on the page.

**Aggregates are structurally unsupported.** Questions like "how many distinct sellers
have a critical ticket?" cannot be answered correctly: the templates return rows, not
aggregates, and the answer agent counts what it was handed. Refusal recall of 65.5%
mostly reflects this.

**Prompt-injection resistance is unproven, not passed.** The planner refuses injected
instructions in the question (verified live). Whether the *answer agent* obeys a
poisoned document that reached the corpus by another route was never established — the
test payload scored 0.5185 against a rank-5 cutoff of 0.5642 and was never retrieved,
even at limit=10. `docs/M6_FINDINGS.md` records it as **unproven**.

**Replanning has never recovered a failure.** It triggers on 1 of 61 answered items and
recovered 0. It is kept because the mechanism is correct and the trigger is rare, but it
has no measured benefit.

**The deployed demo is demo-only.** It replays eight recorded runs; it has no database
and no model. The live path runs locally and in the test suite, not at that URL. The
full-stack VPS deployment is written (`deploy/`) and **has never been run**.

**8 username-class credential defaults remain.** M6 removed every password default; the
usernames still fall back to a literal, so with no environment at all the query path
would connect as the schema owner. Asserted in
`tests/test_postgres_readonly_role.py::test_without_the_readonly_role_the_runtime_falls_back_to_the_owner`.

**9.4% of documents are truncated at embedding time** (F-10), and authored fields never
reach the embedded text. Documented, deferred, not fixed.

**The corpus is 6,098 documents, not 8,152.** The larger figure was retired
deliberately in `docs/CORPUS_DESIGN.md` during M1 when the entity-linkage rebuild
dropped low-signal records. It should not be quoted again.

**53 routing questions is a small sample**, one annotator calibrated the judge, and the
whole corpus is one dataset in one language.

---

## Deployment

| | |
|---|---|
| UI | https://enterprise-ai-demo-ui.onrender.com |
| API | https://enterprise-ai-demo-api-77mb.onrender.com |

`DEMO_MODE=true` replays recorded runs. **Zero LLM calls and zero database queries is
structural, not promised**: in demo mode the API never imports the workflow modules, and
`requirements-demo.txt` does not install torch, `google-genai`, or any database driver —
verified inside the built image. The demo image is 294 MB.

Free-tier caveats, stated rather than discovered: instances spin down after 15 minutes
idle and take about a minute to wake, and Render documents the free tier as unsuitable
for production.

Local, full stack:

```bash
cp .env.example .env            # fill in every value; nothing has a working default
docker compose up -d            # postgres, neo4j, qdrant, api, ui
pytest -q                       # 312 deterministic tests; 4 more need GEMINI_API_KEY
```

`docs/SETUP.md` has the data-loading order. Images are multi-stage and run as a
non-root user; `tests/test_container_hygiene.py` fails the build if that regresses.

---

## Reproducing the numbers

| Claim | Command |
|---|---|
| Every audited claim | `python scripts/audit_claims.py` |
| The published verdict table | `python scripts/publish_verdict_table.py` |
| The test suite | `pytest -q` |
| Routing accuracy, cost, refusal rates | `docs/M3_RESULTS.md`, `tests/eval/results/` |
| Cost table | `docs/M5_COST_TABLE.md` |
| Container hygiene, before/after | `python scripts/verify_container.py <before> <after>` |
| Replay a recorded run | `python scripts/replay_run.py --item C33` |

---

## Documents

| | |
|---|---|
| `AUDIT.md` | The original audit. 110 claims, 11 contradicted |
| `REBUILD_PLAN.md` | The ten milestones and their exit criteria |
| `docs/ENGINEERING_NOTES.md` | What each finding was, and what changed |
| `docs/M2_CHANGES.md` … `docs/M8_FINDINGS.md` | Per-milestone measurements |
| `docs/CORPUS_DESIGN.md` | Why the corpus is 6,098 records |
| `docs/AUDIT_2026-09.md` | The re-audit verdict table, generated from the run |
| `audit/claims.json` | The claim manifest, with a check bound to each |

Built with Gemini, LangGraph, PostgreSQL, Neo4j, Qdrant, FastAPI, Streamlit and
OpenTelemetry.
