# Enterprise Knowledge Platform — rebuild plan

Ten milestones. Strictly ordered. Each has an exit criterion that is a **number or
a passing test**, never "done" or "looks good."

The ordering rule: **nothing that claims to improve quality gets built before the
thing that can measure quality.** Reranking, replanning, and caching all move to
after M3 for this reason — until there is a held-out evaluation set, "the
reranker helped" is an opinion.

---

## M0 — Freeze the baseline and build the safety net

Nothing is improved in this milestone. The point is to make later change
measurable and reversible.

- Capture current behaviour as a frozen baseline artefact: the 5 existing cases,
  the 3 document paraphrases, the `"purple monkey dishwasher"` control, and the
  crashing revenue question. Record routes, evidence hashes, answers, scores.
  Every later milestone is scored against this.
- Introduce `pytest`, `pyproject.toml`, `__init__.py`, and a GitHub Actions
  workflow that actually runs tests. Verify the workflow fails when a test fails
  — a green CI that never ran is worse than no CI.
- Pin every dependency in `requirements.txt` (currently zero pins across 20
  packages) and pin the HuggingFace model revision.
- Commit the data-cleaning step. It currently lives in a gitignored notebook, so
  the first stage of the pipeline is not in the repository.
- Remove the 8 committed `.pyc` files and fix the `!src/**` rule in `.gitignore`
  that lets them through.
- Document the real setup path end to end, then verify it by cloning into a fresh
  directory and following your own README with no prior knowledge.

**Exit:** a clean clone reaches a working stack using only committed files and
documented commands. CI green, and demonstrably capable of going red.

---

## M1 — Rebuild the data foundation

This is the milestone most likely to be skipped and the one that invalidates
everything downstream if it is.

- **Regenerate the synthetic corpus so it carries independent information.**
  Today it is a deterministic `ORDER BY ... LIMIT` over a PostgreSQL view, so
  every ticket sits on `review_score = 1, is_late_delivery = True` — zero
  variance — and the vector layer mirrors data the SQL layer already has. The
  corpus must contain what the structured tables cannot express: root causes,
  resolution paths, agent notes, policy exceptions, escalation history. Severity
  must vary. Some complaints must land on well-rated sellers and some well-rated
  sellers must have unresolved warranty issues, or there is nothing for retrieval
  to discover.
- Link policies to sellers. The flagship question asks for sellers associated with
  relevant support policies; that association does not currently exist in the data.
- Make generation deterministic: add a unique tiebreaker to every `ORDER BY`, seed
  `numpy.random` as well as `random`, and freeze `created_at` from a seed rather
  than `datetime.now()`. Verify by running twice and diffing to zero.
- Fix the loader: `sentiment_label` is NULL for all 99,224 reviews and
  `product_category_name_english` for all 73 categories. Fix the cause —
  `standardize_columns` silently inventing missing columns — so schema drift
  fails loudly instead of propagating NULLs into LLM prompts.
- Decide what to do about `customer_message`: 100% of ticket messages are verbatim
  Portuguese Olist review text, embedded by an English-only model, 70 of them with
  double-encoded UTF-8. Either switch to a multilingual embedding model, or
  generate genuinely synthetic English messages, or translate. Pick one and say
  why in the docs.
- Handle the 9.4% of documents silently truncated at 256 word-pieces (74% of
  troubleshooting guides).

**Exit:** regeneration is bit-identical across runs; no NULL-filled columns reach
any prompt; and a documented statistic shows the synthetic layer's signal is not
recoverable from the structured tables alone.

---

## M2 — Make retrieval actually retrieve

The three defects that void the current architecture.

- **F-03:** parameterise the SQL and Cypher templates. The planner must extract
  entities, filters, and constraints from the question and bind them; today no
  template accepts anything but `:limit`, so a business question and
  `"purple monkey dishwasher"` return byte-identical evidence. Keep the allowlist
  — it is genuinely good and it held 8 of 8 adversarial prompts. Allowlist the
  *shape*, bind the *values*.
- **F-01:** carry entity IDs into the Qdrant payload. Ingest reads top-level record
  keys while every ID lives under `linked_entities`, so 0 of 8,152 points can join
  back to SQL or graph evidence.
- **F-02:** the retriever reads `text_preview`; ingest writes `text`. Fix the
  mismatch so document text actually reaches the LLM.
- Allow SQL-only and graph-only plans. `"Which seller had the highest revenue?"`
  currently crashes because the plan schema requires a non-null graph intent.
- Add `ORDER BY` to the graph templates that have none, and fix the one ordering a
  string severity column (`medium > low > high > critical`).
- **Add boundary tests between stages.** This is the structural lesson from the
  audit: twelve validators all reported PASS while the retrieval layer was inert,
  because each validated its own stage in isolation and none covered the
  JSONL → Qdrant handoff where the IDs were lost. Every stage boundary gets a test
  asserting the contract, not just the row count.

**Exit:** two different questions provably return different evidence; a vector hit
joins to a SQL row by entity ID in an automated test; the `"purple monkey
dishwasher"` control returns either different evidence or an explicit refusal.

---

## M3 — Evaluation (the gate for everything after)

- Build a **held-out** question set of 60–100 items. Not the planner's few-shot
  examples — the current five test cases are byte-identical to the prompt's own
  worked examples, and the same five are counted three times across three
  validators. Label each with expected routes, expected source records, and a
  reference answer.
- Metrics: planner routing accuracy, retrieval precision/recall@k, groundedness,
  hallucination rate, refusal correctness on unanswerable questions.
- Replace the deterministic gate. It currently reduces to `len(answer) >= 1000`.
  Real deterministic assertions: every cited entity ID exists in the retrieved
  evidence; every number in the answer appears in a retrieved record.
- **Calibrate the judge.** Hand-label a blind subset, compute Cohen's kappa against
  the LLM evaluator, and report it — including if it is below 0.7. You have done
  this before; do it again here.
- **Build a single-agent baseline.** One prompt, all three databases, no planner.
  If the multi-agent pipeline does not beat it on the held-out set, that is a
  finding worth publishing, not a failure to hide.
- Add adversarial and injection scenarios to the set, and a control question with
  no valid answer.

**Exit:** every metric above has a number with a confidence interval, produced by
a command anyone can re-run.

---

## M4 — Reliability

Now, and only now, the loop.

- Evaluator-triggered replanning, capped at one retry. Measure on the M3 set: what
  fraction of failures does it recover, at what added latency and cost? If it does
  not help, say so and keep the cap at zero.
- Retries with backoff and a circuit breaker on each external dependency.
- Timeouts on every DB and LLM call.
- Graceful degradation: if Neo4j is down, answer from SQL and Qdrant and *say so*
  in the response, rather than 500ing.
- Fail closed, not open. `agentic_workflow.py` currently defaults the workflow
  status to `PASS` when the evaluation summary is missing, and two report writers
  hardcode `"overall_status": "PASS"` as a literal.
- Delete the dead code the trace lies about: the observability log records
  `operation: "plan_query_route"` for a function that is never called.

**Exit:** a chaos test kills each dependency in turn; the system degrades with a
correct message and no 500, and the replanning decision is backed by a measured
delta.

---

## M5 — Cost, latency, and caching

- Token accounting per agent call; cost per query; p50/p95/p99 latency per stage.
- Load the embedding model once at startup. It is currently re-read from disk on
  every single request.
- Caching, with a measured hit rate and spend reduction — not just "caching added."
- A reranker **only if** M3 shows retrieval precision is the bottleneck. If
  precision@5 is already high, a reranker is decoration and adds latency.
- Concurrency test: N parallel queries, report throughput and error rate.

**Exit:** a table of cost and latency per stage, before and after, on the same
eval set.

---

## M6 — Security

- A non-superuser, read-only PostgreSQL role. `enterprise_user` currently has
  `Superuser, Create role, Create DB, Replication, Bypass RLS`.
- `execute_read` instead of `session.run()` for Neo4j; read-only transaction
  options on SQLAlchemy instead of `engine.begin()`, which commits.
- An API key on Qdrant. Anything on the network can currently drop the collection —
  the audit upserted a poisoned document with no credentials and watched it rank
  first for the flagship question and reach the answer prompt verbatim.
- Authentication on the API; sanitised error responses (exception detail with host
  and port is currently returned to unauthenticated callers).
- A real `/health` that checks dependencies. It is currently a static literal that
  returned `ok` while all four dependencies were unreachable and `/query` was
  500ing on every request.
- Prompt-injection defence on retrieved content, with the injection scenarios from
  M3 as the test.
- Remove the working passwords that sit as source-code defaults in 17 places.

**Exit:** the write probes from the audit are re-run and all fail; injection
scenarios pass; `/health` goes red when a dependency dies.

---

## M7 — Observability

- OpenTelemetry spans instead of bespoke JSONL, with cost and tokens as span
  attributes.
- Spans that cannot silently truncate: a crash *between* spans currently leaves a
  trace where every recorded event is marked PASS.
- Deterministic replay of a recorded run.
- A trace viewer, or export to a standard backend.

**Exit:** a failing run is diagnosable from the trace alone, without re-running it.

---

## M8 — Deployment

- Multi-stage Dockerfile, non-root user, `.dockerignore` verified.
- Cloud deployment with a public URL.
- A demo mode that replays recorded runs so visitors need no API key and you burn
  no budget.
- UI: show the evidence behind every claim — which records, which IDs, which
  routes — plus cost and latency per query. The evidence trail *is* the product.

**Exit:** someone with the link can use it, and you can leave it running.

---

## M9 — Documentation, and a second audit

- README as a design doc: what the system is, what it does, what it measured, what
  it cannot do.
- An engineering notes document carrying the audit findings and what changed. The
  before/after is the most credible thing you will have.
- **Re-run the claim audit** against the finished system, the same way, and publish
  the verdict table. First audit: 11 contradicted out of 110. Target: zero, with
  every remaining caveat stated in the README itself.

**Exit:** zero contradicted claims, machine-checked.

---

## Things worth deciding early

- **Scope honestly.** This is roughly 8–12 weeks part-time. M0–M3 is the half that
  changes whether the project is defensible; M4–M9 is the half that makes it
  impressive. If time runs short, ship M0–M3 complete rather than all ten at half
  depth.
- **"No mistakes" is not the target and is not achievable.** The target is: every
  claim in the documentation is true, and every number is measured and
  reproducible. A stated limitation costs nothing in an interview. A false claim
  costs the interview.
- **Re-audit at M3 and at M9, not just at the end.** The reason this plan exists is
  that a confident write-up was wrong twice.
