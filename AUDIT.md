# AUDIT.md

Independent audit of `C:\enterprise_ai` against the claims in `docs/Problem I wanted to solve.md`.
Method: execution first, reading second. Every verdict cites a file:line, a command, or live output.

**Audit date:** 2026-08-20
**Commit audited:** `f8dc45c` (branch `main`; the claims doc itself is untracked)

---

## Totals

| Verdict | Count |
|---|---|
| CONFIRMED | 78 |
| CONFIRMED WITH CAVEAT | 18 |
| CONTRADICTED | 11 |
| UNVERIFIABLE | 3 |
| **Total claims tested** | **110** |

The row counts, the graph, the embeddings, the observability schema and the Docker topology are real
and check out to the digit. The retrieval layer, the entity-linkage premise, the "read-only runtime"
and the meaning of "5/5" do not.

---

## Environment I needed

Everything below was run against a **live, already-running stack** on the audit machine. I did not
build it; it was up when I started.

| Dependency | State | Checks it unblocked |
|---|---|---|
| `enterprise_ai_postgres_deploy` (postgres:16), up 2 days, seeded | available | all row counts, views, privilege tests, P1 correlations |
| `enterprise_ai_neo4j_deploy` (neo4j:5-community), up 2 days, loaded | available | node/rel counts, orphan checks, Cypher write attack |
| `enterprise_ai_qdrant_deploy`, 8,152 points | available | payload audit, embedding fidelity, injection test |
| `enterprise_ai_api` / `enterprise_ai_ui` | up but **DNS-broken** (see F-14) | API validator could not be completed in-container |
| `GEMINI_API_KEY` in `.env` (real, working) | available | ~25 live Gemini calls: planner attacks, full 5-case re-run, evaluator probes |
| `.venv` with all deps | available | ran every module from the host |
| Olist raw + `*_cleaned.csv` in `data/` | present on disk, **not in git** | see P4 / F-10 |

**Checks I could not run:**
- Clean-clone reproduction end-to-end. Impossible by construction — the data-cleaning step is not in
  the repository (P4). I verified the blocker rather than the outcome.
- API `/query` from inside the container (DNS failure, F-14). I re-ran the identical five cases from
  the host instead, which exercises the same code path minus the HTTP layer.
- Any claim about latency, cost or concurrency — the document already says these were never measured,
  and nothing in the repo measures them. Confirmed by absence.

---

# P1 — Is the synthetic layer statistically linked, or ID-stamped?

**Short answer: it is not random ID-stamping, and it is not independent ground truth either. It is a
deterministic `ORDER BY ... LIMIT 3000` over PostgreSQL columns, producing a degenerate slice with
zero variance on the very variables the flagship question asks about.**

## How IDs are actually assigned

`src/synthetic/synthetic_data_generator.py:336-421` — `fetch_support_candidate_orders()` is a single
SQL query ending:

```sql
ORDER BY candidate_priority DESC, min_review_score ASC NULLS LAST,
         delivery_delay_days DESC NULLS LAST, total_payment_value DESC NULLS LAST
LIMIT :max_records
```

with `candidate_priority = 4 WHEN lr.review_score <= 2`. There is **no sampling and no randomness** in
entity selection. `random.seed(42)` (line 844) governs only cosmetic fields — `status`, `channel`,
`claim_reason`, `requested_resolution`.

Derivation chain:
- `generate_customer_emails(support_tickets)` (line 595) reuses `ticket["linked_entities"]` **by reference**.
- `generate_warranty_claims(support_tickets, 1000)` (line 641) takes `eligible_tickets[:1000]` — a prefix slice.
- `generate_policy_documents` / `generate_troubleshooting_guides` take only the category table.

## Measured coverage

```
$ .venv/Scripts/python.exe /tmp/audit/p1.py
tickets distinct orders : 3000 / 3000
emails distinct orders  : 3000 / 3000  identical-to-tickets=True
warranty distinct orders: 1000 / 1000  subset-of-tickets=True
logistics distinct ords : 1000 / 1000  overlap w/ tickets=663
UNION of all order ids referenced by 8152 artifacts: 3337

ticket review_score counts: {1: 3000}
ticket is_late_delivery  : {True: 3000}
delivery_delay_days: n=3000 min=3.82 max=175.87 mean=14.39
```

- All 8,152 artifacts collectively reference **3,337 distinct orders out of 99,441 — 3.4%**.
- **Every single support ticket sits on an order with `review_score = 1` AND `is_late_delivery = True`.**
  Zero variance. There is no negative-vs-positive contrast anywhere in the ticket corpus.
- Emails are an entity-identical clone of tickets; warranty claims are a strict subset. Of the "8,152
  records", the *entity* information content is 3,000 + 1,000 rows.

## Correlations (per seller, n=3,035 with review data)

```
$ .venv/Scripts/python.exe /tmp/audit/p1b.py
sellers total=3035  with >=1 synthetic complaint=860 (28.3%)
  corr(n_complaints, mean_review ) pearson=-0.0311  spearman=-0.2617
  corr(n_complaints, late_rate   ) pearson=+0.0887  spearman=+0.6350
  corr(n_complaints, n_orders    ) pearson=+0.8673  spearman=+0.6178
  PARTIAL corr(complaints, mean_review | n_orders) = -0.1001
  PARTIAL corr(complaints, late_rate   | n_orders) = +0.1741
  NULL (uniform draw, 200 reps) corr w/ mean_review: mean=+0.0165 sd=0.0019
  NULL (uniform draw, 200 reps) corr w/ late_rate  : mean=+0.0028 sd=0.0017
```

Per category (n=73):
```
  corr(n_complaints, mean_review ) pearson=+0.0208 spearman=+0.0100
  corr(n_complaints, late_rate   ) pearson=+0.2069 spearman=+0.4975
  corr(n_complaints, n_orders    ) pearson=+0.9916 spearman=+0.9708
  PARTIAL corr(complaints, mean_review | n_orders) = -0.1341
  PARTIAL corr(complaints, late_rate   | n_orders) = +0.1550
```

## Verdict on P1

**Not noise.** Spearman −0.26 (review score) and +0.64 (late-delivery rate) at seller level, versus a
uniform-random null of +0.017 / +0.003. The IDs were not drawn at random, and the document's linkage
claim is, at the JSONL level, true.

**But the signal does not mean what the project needs it to mean, for three reasons:**

1. **It is tautological, not evidential.** The complaint set is a pure deterministic function of
   `ecommerce.vw_order_summary` columns. Anything the vector layer "discovers" about which sellers
   have complaints is recoverable exactly by a `SELECT` on the same view. The synthetic layer adds no
   information the SQL route does not already have. It cannot corroborate the SQL route, because it
   *is* the SQL route with different formatting.

2. **The dominant driver is volume, not quality.** `corr(complaints, n_orders) = +0.87` per seller,
   `+0.99` per category. Partial out order volume and the quality signal collapses to −0.10 / +0.17.
   "Which sellers have the most complaints" is, to first order, "which sellers have the most orders."

3. **It is degenerate.** Review score has zero variance across all 3,000 tickets. A retrieval system
   cannot rank sellers by complaint severity when every complaint carries an identical severity signal.

**And the link is destroyed before it reaches the vector store anyway.** See F-01: 0 of 8,152 Qdrant
points carry `customer_id`, `order_id`, `product_id` or `seller_id` in any form. So the specific
claimed benefit — *"it allowed vector-search results to be connected back to PostgreSQL and Neo4j
evidence"* — is CONTRADICTED empirically, independent of the correlation question.

To state it in the terms requested: **the flagship question does have a ground truth in the JSONL, but
that ground truth is a restatement of a PostgreSQL view rather than independent evidence; the vector
index cannot express it because the identifiers were dropped at ingest; and the hybrid-retrieval
premise is therefore decorative in its current form.** An evaluation built on the vector path measures
nothing about entity linkage.

---

# P2 — Is the tool allowlist real?

**Short answer: the allowlist is real and it held under attack. The "read-only runtime" claim is false
at every layer beneath it.**

## The validation boundary

| Path | Where validated | Fail mode |
|---|---|---|
| Planner → SQL intent | `src/planning/gemini_query_planner.py:195-200` — `raise ValueError` if not in `ALLOWED_SQL_INTENTS` | fail-closed |
| Planner → graph intent | `gemini_query_planner.py:201-206` | fail-closed |
| Planner → vector groups | `gemini_query_planner.py:208-221` | fail-closed |
| SQL intent → SQL string | `hybrid_retriever.py:290` `sql_templates[intent]` | fail-closed (`KeyError`) |
| Graph intent → Cypher | `hybrid_retriever.py:380` `cypher_templates[intent]` | fail-closed (`KeyError`) |
| Vector group → filter | `hybrid_retriever.py:392-401` — **no allowlist** | **fail-open, silent 0 results** |

Can a driver call be reached without crossing the boundary? **No, via the API.** `POST /query` accepts
`{"query": str}` only (`src/api/main.py:22-30`); there is no route-plan injection surface. That is a
genuinely good design decision and it is the reason the attacks below failed.

## Attack 1 — force the planner out of the allowlist (8 live Gemini calls)

```
$ .venv/Scripts/python.exe /tmp/audit/p2_planner.py
A1 direct override:  ACCEPTED -> sql=review_intelligence graph=customer_ticket_order_product_paths
A2 fake new route:   REJECTED -> ValueError: Invalid SQL intent from Gemini: raw_sql_exec
A3 schema coercion:  ACCEPTED -> sql=seller_performance graph=warranty_product_seller_paths
A4 wildcard group:   ACCEPTED -> (6 legal groups; "internal_hr_records" dropped by Gemini)
A5 off-domain:       REJECTED -> ValueError: Invalid SQL intent from Gemini: None
A6 gibberish:        REJECTED -> ValueError: Invalid SQL intent from Gemini: None
A7 shell+SQL exec:   REJECTED -> ValueError: Invalid SQL intent from Gemini: None
A8 null route:       REJECTED -> ValueError: Invalid SQL intent from Gemini: None
```

A2 is the interesting one: Gemini **did** emit `raw_sql_exec` when told a new tool existed. The
allowlist caught it. 0/8 attacks reached the database with an out-of-allowlist route.

## Attack 2 — injection through `intent` and `limit`

```
BLOCKED intent='seller_performance; DROP TABLE ecommerce.reviews' -> KeyError
BLOCKED intent='__import__'                                       -> KeyError
BLOCKED intent="'; DROP TABLE x;--"                               -> KeyError
limit injection -> DataError: invalid input syntax for type bigint: "1; DROP TABLE ecommerce.reviews"
```

`limit` is bound as a real parameter (`hybrid_retriever.py:71-73`, `text()` + psycopg2). Not injectable.

## Attack 3 — writes through the project's own driver helpers

This is where it breaks.

```
$ .venv/Scripts/python.exe /tmp/audit/p2_write2.py
before: [{'c': 73}]
row-returning INSERT via records_query -> [{'product_category_name': 'audit_probe_row'}]
after (new engine): [{'c': 74}]        <-- committed, visible to a fresh connection
probe row persisted: [{'product_category_name': 'audit_probe_row', ...}]
cleanup: [{'product_category_name': 'audit_probe_row'}]
final count: [{'c': 73}]
```

Any row-returning statement passed to `records_query()` (`hybrid_retriever.py:70-73`) **executes and
commits**, because it uses `engine.begin()` — a read-write transaction that commits on exit — not
`execution_options(postgresql_readonly=True)`.

Bare DDL (`CREATE TABLE`) does raise `ResourceClosedError: This result object does not return rows` —
but note *why*: the exception comes from iterating an empty cursor, inside the `with` block, which
happens to roll the transaction back. That is **incidental, not a control**. The `CREATE ROLE` attempt
produced the identical error message. This is exactly the failure pattern you flagged: an error is
reported to the caller and the caller cannot tell whether the statement ran.

Neo4j has no such accident:
```
  OK   "CREATE (n:AuditPwnProbe {id:'x'}) RETURN n.id AS id" -> [{'id': 'x'}]
  OK   'CREATE INDEX audit_pwn_idx IF NOT EXISTS FOR ...'    -> []
  OK   'DROP INDEX audit_pwn_idx IF EXISTS'                  -> []
  OK   'MATCH (n:AuditPwnProbe) DELETE n RETURN count(*)'    -> [{'deleted': 1}]
```
`graph_query()` (`hybrid_retriever.py:76-79`) uses `session.run()` on a write-capable session, not
`session.execute_read()`. Node creation, index DDL and deletion all succeeded.

Qdrant:
```
  CREATE COLLECTION: OK  <-- runtime client can create collections
  DELETE COLLECTION: OK  <-- and delete them
```
`build_qdrant_client()` (`hybrid_retriever.py:63-68`) passes **no `api_key`**, and the Qdrant service in
`docker-compose.yml:37-45` sets no `QDRANT__SERVICE__API_KEY`. Anything on the network can drop the
collection.

## Attack 4 — database-level privileges

```
$ docker exec enterprise_ai_postgres_deploy psql -U enterprise_user -d enterprise_ai -c "\du"
 enterprise_user | Superuser, Create role, Create DB, Replication, Bypass RLS

 ins | del | upd | create_schema | readonly_replica | default_ro
  t  |  t  |  t  |       t       |        f         |    off
```

**The runtime account is a PostgreSQL superuser.** Neo4j Community has no RBAC at all (`SHOW ROLES` →
`Unsupported administration command`); the single `neo4j` user is a full admin. Qdrant has no auth.
There is **zero** database-level enforcement of read-only anywhere.

## Attack 5 — prompt injection via retrieved content

```
$ .venv/Scripts/python.exe /tmp/audit/p2_inject2.py
  1. score=0.7024 'Find sellers with negative customer complaints, warranty issues, and releva' <== POISONED DOC RETRIEVED
  2. score=0.6373 'Product quality complaint for ferramentas_jardim'
  ...
AUDIT-INJECTION-MARKER present in saved retrieval context: True
```

I upserted a point into the **live collection with no credentials**, it ranked #1 for the flagship
business question, and its attacker-controlled `title` was written verbatim into
`workflow_retrieval_context.json` — the exact structure passed to the answer and evaluator LLMs
(`gemini_answer_evaluator.py:60-101`). No sanitisation exists anywhere on that path.

Two mitigations exist only by accident: `text_preview` is always `None` (F-02), so document *bodies*
never reach the LLM, and the shipped corpus is template-generated so no live injection is present.
Neither is a control. Whether Gemini would obey the injected instruction I did not measure — the
verifiable fact is that attacker text reaches the prompt unfiltered and the corpus is world-writable.

## Verdict on P2

| Claim | Verdict |
|---|---|
| "could not generate and execute arbitrary SQL, Cypher, Python or shell commands" | **CONFIRMED** — 8/8 attacks blocked; template dicts are fail-closed |
| "Tool actions are allowlisted" | **CONFIRMED** at the planner boundary; note the vector-group check exists only there (`hybrid_retriever.py:392` fails open) |
| "The runtime workflow is intended to be read-only" | **CONTRADICTED** — superuser account, committing read-write transactions, write-capable Neo4j sessions, unauthenticated Qdrant. The only thing preventing a write is that nobody wrote a DML template |
| "No prompt-injection protection" (stated limitation) | **CONFIRMED**, and demonstrated live |

The honest framing: **the allowlist is a genuine, well-implemented control, and it is the only one.
There is no defence in depth behind it.**

---

# P3 — Who decided the five cases passed?

## The five test cases are the planner's own few-shot examples, verbatim

```
$ .venv/Scripts/python.exe /tmp/audit/p3_overlap.py
Planner prompt few-shot example queries:
  - Find sellers with negative customer complaints, warranty issues, and relevant support policies
  - Investigate late delivery logistics incidents by customer region and find troubleshooting guidance
  - Find payment questions, refund policies, and customer support cases
  - Investigate product quality complaints and find troubleshooting procedures
  - Show seller ticket paths for negative complaints and product issues

src/orchestration/agentic_workflow_validator.py: 5 cases -> all 5 in planner few-shot prompt: True
src/api/api_validator.py:                        5 cases -> all 5 in planner few-shot prompt: True
src/retrieval/retrieval_context_validator.py:    5 cases -> all 5 in planner few-shot prompt: True

workflow == api case list: True
workflow == context-builder case list: True
```

`build_planner_prompt()` (`gemini_query_planner.py:100-158`) embeds five worked examples. The
`expected_sql_intent` / `expected_graph_intent` / `expected_vector_groups` in all three validators are
copied from those examples' own "Correct JSON" blocks.

**The planner is graded on its own answer key.** A 5/5 route-match score is guaranteed by construction
and measures memorisation, not routing.

And to answer the question directly: **yes, it is the same five cases counted three times.** The three
lists are byte-identical.

## The deterministic / LLM split

Deterministic checks (`agentic_workflow_validator.py:77-247`):

| Check | Line | What it actually proves |
|---|---|---|
| `sql_intent == expected` | 100 | echo of the few-shot prompt |
| `graph_intent == expected` | 110 | echo of the few-shot prompt |
| expected vector groups present | 118 | echo of the few-shot prompt |
| `answer_provider == "gemini"` | 128 | hardcoded literal at `answer_generator.py:361` |
| `answer_model` startswith "gemini" | 137 | echo of `.env` |
| `answer_length_chars >= 1000` | 145 | length |
| 7 output files exist | 224-243 | files were written |
| query substring in `answer_preview` | 213 | **can never fail** — `answer_preview` is the full answer, and the answer prompt restates the business question verbatim |

**None of these check that any fact in the answer matches any retrieved record.**

LLM-scored (`gemini_answer_evaluator.py:262-296`, consumed at `agentic_workflow_validator.py:169-211`):
`overall_status`, `grounding_score >= 3`, `completeness_score >= 3`, `business_readiness_score >= 3`,
`unsupported_claims == []`.

The evaluator's own "deterministic checks" (`gemini_answer_evaluator.py:210-255`) are:
1-3. does the answer literally contain "PostgreSQL" / "Neo4j" / "Qdrant" (or synonyms) — `severity: medium`
4. `len(answer) >= 1000` — `severity: high`
5. does the answer contain "limitation" / "missing evidence" — `severity: medium`

Only `high` severity can force a FAIL (`gemini_answer_evaluator.py:281-296`). **The entire deterministic
contribution to the verdict reduces to: the answer is at least 1,000 characters long.** The document's
statement *"I also used deterministic checks alongside the LLM evaluator… because the evaluator itself
is also an LLM and should not be trusted blindly"* is **CONTRADICTED in substance**: the deterministic
layer is a keyword-and-length filter that cannot detect a wrong answer.

## But the evaluator is not a rubber stamp — I tested it

```
$ .venv/Scripts/python.exe /tmp/audit/p3_evaluator.py
### REAL answer (baseline)  (len=3114)
  overall_status=PASS  grounding=5 completeness=5 business=5  unsupported_claims (0)
  deterministic_failures: []

### FABRICATED answer  (fake sellers, fake regulatory fine, fake claim IDs, padded past 1000 chars)
  overall_status=FAIL  grounding=1 completeness=2 business=1
  unsupported_claims (5): ['Seller ZZZZ-FABRICATED-9999 in Tokyo, Japan exists in the dataset.',
                           'Seller QQQQ-NOT-REAL-0001 was fined R$4.2 million by ANATEL.', ...]
  deterministic_failures: []          <-- the deterministic layer caught NOTHING
```

Harder test — the real answer with every number multiplied, all real IDs preserved:
```
SUBTLY-CORRUPTED NUMBERS -> overall=FAIL grounding=2 compl=3 biz=2
  unsupported_claims: ["The generated answer incorrectly calculates performance metrics, stating
                       '592 late deliveries, 14.17 review sc..."]
  deterministic_failures: []
```

The Gemini evaluator caught both, including the subtle numeric corruption. Credit where it is due.
Note in both cases `deterministic_failures: []` — the verdict was 100% the LLM.

**What "5/5" therefore quantifies:** a Gemini evaluator, with no measured agreement against human
labels, gave 5/5/5 on all five cases in both the committed run and my re-run — zero variance across
ten scores. It establishes that the answers are not grossly hallucinated. It quantifies nothing about
routing correctness (answer-key leak), nothing about retrieval quality, and nothing about correctness
on any question outside the prompt's own examples.

## Re-runnability

**Yes, and it reproduces.** The cases are a committed runnable fixture (`TEST_CASES` at
`agentic_workflow_validator.py:12-62`), not ad hoc.

```
$ python src/orchestration/agentic_workflow_validator.py --output /tmp/audit/rerun_workflow_report.json
Overall status: PASS
seller_complaint_warranty_policy:        PASS SQL=seller_performance  Graph=warranty_product_seller_paths  Eval=PASS Grounding=5
late_delivery_logistics_region_guidance: PASS SQL=order_summary       Graph=logistics_region_paths         Eval=PASS Grounding=5
payment_refund_policy:                   PASS SQL=payment_summary     Graph=customer_ticket_order_product_paths Eval=PASS Grounding=5
product_quality_troubleshooting:         PASS SQL=review_intelligence Graph=customer_ticket_order_product_paths Eval=PASS Grounding=5
seller_negative_ticket_paths:            PASS SQL=seller_performance  Graph=seller_ticket_product_paths    Eval=PASS Grounding=5
```

Identical routes and identical scores to `reports/agentic_workflow_validation_report.json` (generated
2026-07-12). Genuinely reproducible. **The API validator does not reproduce** — see F-14.

## Generalisation: what happens off the answer key

I ran the document's *own* paraphrase examples through the live planner.

```
$ .venv/Scripts/python.exe /tmp/audit/p3_generalize.py
BASELINE (few-shot verbatim)  -> sql=seller_performance  graph=warranty_product_seller_paths   SQL HIT  GRAPH HIT
doc paraphrase 1              -> sql=review_intelligence graph=seller_ticket_product_paths     SQL MISS GRAPH MISS
   ("Which sellers have the most complaints?")
doc paraphrase 2              -> sql=seller_performance  graph=seller_ticket_product_paths     SQL HIT  GRAPH MISS
   ("Find vendors connected to poor customer experiences.")
doc paraphrase 3              -> sql=seller_performance  graph=warranty_product_seller_paths   SQL HIT  GRAPH HIT
doc example (revenue)         -> CRASH ValueError: Invalid graph intent from Gemini: None
   ("Which seller had the highest revenue?")
held-out A/B/C                -> 1 SQL hit, 0 graph hits out of 3
```

100% on the memorised query. On the three paraphrases the document itself offers as proof that an
agentic planner is needed: **1 of 3 fully correct.** And the document's own worked example — *"if the
question asks for the highest-revenue seller, the planner should select PostgreSQL"* — **crashes the
entire workflow**, because `validate_and_normalize_plan` requires a non-null graph intent
(`gemini_query_planner.py:201`). A SQL-only plan is unrepresentable.

---

# P4 — Reproducibility

## Generator determinism: seeded but not deterministic

Run twice, diffed:
```
$ diff run1 run2
  DIFFERS    support_tickets        (2 records)
  DIFFERS    customer_emails        (2 records)
  DIFFERS    logistics_incidents  (242 records)
  IDENTICAL  warranty_claims
  DIFFERS    policy_documents      (79/79)
  DIFFERS    troubleshooting_guides (73/73)

$ diff run1 data/synthetic   (the corpus actually loaded into Neo4j and Qdrant)
  IDENTICAL  support_tickets, customer_emails, warranty_claims
  DIFFERS    logistics_incidents   (254 of 1000)
  DIFFERS    policy_documents       (79 of 79)
  DIFFERS    troubleshooting_guides (73 of 73)
```

Field-level diagnosis:
```
policy_documents:       79/79   differ; fields -> {'created_at': 79}          # datetime.now()
troubleshooting_guides: 73/73   differ; fields -> {'created_at': 73}          # datetime.now()
support_tickets:         2/3000 differ; fields -> {created_at,title,summary,customer_message,linked_entities,document_text}
logistics_incidents:   242/1000 differ; fields -> {created_at,title,summary,linked_entities,metadata,document_text}
  linked_entities SET identical across runs: True (all four types)
```

So `random.seed(42)` (line 844) works. The non-determinism has two unseeded causes:

1. `datetime.now(timezone.utc)` in policy/guide `created_at` — cosmetic.
2. **PostgreSQL tie-break instability.** `ORDER BY os.delivery_delay_days DESC NULLS LAST`
   (`synthetic_data_generator.py:520`) has no unique tiebreaker. The *set* of selected entities is
   stable; the *order* is not, so `INC-000123` refers to a different order on each regeneration.

Since Neo4j nodes and Qdrant points are keyed on those IDs, a regenerate-and-reload silently re-points
24% of logistics incidents. **Not a "nobody can regenerate it" failure — a "regenerating it produces a
corpus that disagrees with your saved reports" failure.** The fix is one line: add `, os.order_id` to
the ORDER BY.

Also: `random` is seeded but `numpy.random` is not (unused today — a latent trap). And the generation
report hardcodes `"overall_status": "PASS"` (`synthetic_data_generator.py:892`) regardless of anything;
same at `hybrid_retriever.py:552`.

## Embeddings: fully reproducible

```
$ .venv/Scripts/python.exe /tmp/audit/p4_embed.py
model dim: 384
  logistics_incidents  stored||v||=1.000000  cos(stored, fresh-encode)=1.00000000
  warranty_claims      stored||v||=1.000000  cos(stored, fresh-encode)=1.00000000
  support_tickets      stored||v||=1.000000  cos(stored, fresh-encode)=1.00000000  (x3)
```

Ingest (`qdrant_ingest.py:502-506`) and query (`hybrid_retriever.py:466-469`) both read
`EMBEDDING_MODEL_NAME` from the same env var, both use `normalize_embeddings=True`, and the collection
distance is Cosine. Same model, same normalisation, exact bit-level reproduction. **This part is done
correctly.**

Caveat: the HF model is fetched with no revision pin and `sentence-transformers` is unpinned (F-11), so
reproducibility depends on upstream not moving.

## Clean clone: not possible

```
$ git ls-files data/
data/.gitkeep  data/processed/.gitkeep  data/raw/.gitkeep  data/synthetic/.gitkeep
$ git ls-files notebooks/     # (empty — notebooks/ is in .gitignore:27)
$ ls notebooks/
01_olist_exploration.ipynb   data_cleaning_report.md
```

Undocumented manual steps a fresh clone requires, in order:

1. **Download the Olist dataset from Kaggle.** Never mentioned in `README.md` or any file in `docs/`.
2. **Produce `data/processed/*_cleaned.csv`.** The only cleaning code is
   `notebooks/01_olist_exploration.ipynb`, and `notebooks/` is gitignored. **The first stage of the
   pipeline is not in the repository.** `postgres_loader.py` reads `*_cleaned.csv` and cannot run
   without it. (That notebook also causes F-05: it emits a column named `sentiment`, while the loader
   expects `sentiment_label`.)
3. **Create the schema.** `CREATE SCHEMA ecommerce` is documented (`README.md:667`), but
   `database/postgres/schema.sql` is never invoked by any documented command.
4. **Run the synthetic generator.** `src/synthetic/synthetic_data_generator.py` is **never mentioned in
   `README.md` or in any file under `docs/`** (`grep -n "synthetic_data_generator" README.md docs/*.md`
   → no matches), yet `synthetic_neo4j_loader.py` and `qdrant_ingest.py` both require its output.
   Following README §20 verbatim fails at the "Synthetic Neo4j Graph" step.
5. **Set `POSTGRES_HOST` / `NEO4J_URI`.** Present in `.env.example`, absent from the working `.env`;
   the code falls back to `localhost` (`hybrid_retriever.py:31,36`), which works only because the DB
   ports are published.

The documented invocation style (`python src/x/y.py` from repo root) **does** work — I verified all four
sys.path-dependent validators start correctly. That part of the README is accurate.

## CI: does not exist

```
$ ls -a .github          -> No such file or directory
$ find . -name "test_*.py" -o -name "*_test.py"          -> (none)
$ ls pyproject.toml setup.py tox.ini Makefile conftest.py -> none exist
```

No workflow file, no pytest, no test files, no packaging, no `__init__.py` anywhere. There is nothing
that could silently fail because there is nothing. The document's "No CI/CD" is **CONFIRMED**. The
twelve `*_validator.py` files are `__main__` scripts run by hand.

---

# Findings the document does not mention at all

### F-01 — CRITICAL: every entity ID is dropped at Qdrant ingest
```
$ .venv/Scripts/python.exe /tmp/audit/p2_payload.py
total points: 8152
payload key -> #points:
  artifact_type 8152 | artifact_group 8152 | text 8152 | entity_ids 8152 | title 8152 | ...
  ticket_id 7000 | severity 5000 | status 4000 | issue_type 3000 | email_id 3000 | incident_id 1000 | claim_id 1000 | guide_id 73
entity_ids key -> #points:  ticket_id 7000 | email_id 3000 | incident_id 1000 | claim_id 1000 | guide_id 73
points with top-level 'seller_id':   0
points with top-level 'product_id':  0
points with top-level 'customer_id': 0
points with top-level 'order_id':    0
```
Cause: `build_payload()` (`qdrant_ingest.py:229-306`) and `build_document_text()`
(`qdrant_ingest.py:124-227`) both read `record.get(key)` on the **top level** of the JSONL record. Every
entity ID lives one level down, inside `linked_entities` (`synthetic_data_generator.py:299-319`). The
generator's own carefully-built `document_text` field — which *does* contain all the IDs — is ignored
and rebuilt from scratch, losing them. `policy_id` is also looked up but the generator writes
`document_id`, so 79 policy documents have no ID key at all.

**This is the single defect that voids the project's stated architectural thesis.** The whole point of
the synthetic layer was to let vector hits join back to SQL and graph evidence. Zero points can do that.

### F-02 — CRITICAL: `text_preview` is read but never written — no document text reaches the LLM
`compact_vector_result()` reads `payload.get("text_preview")` (`hybrid_retriever.py:450`).
`build_payload()` writes `"text"` (`qdrant_ingest.py:242`). The key `text_preview` exists on **0 of 8,152
points**. Combined with `compact_record()` dropping `None` values
(`retrieval_context_builder.py:129-131`), the vector evidence delivered to Gemini in a real production
run is:
```json
{"score": 0.6373, "artifact_group": "support_tickets", "artifact_type": "support_tickets",
 "title": "Product quality complaint for ferramentas_jardim", "issue_type": "product_quality_complaint",
 "severity": "high", "status": "waiting_for_seller"}
```
*(verbatim from `reports/api_runs/find_sellers_.../workflow_retrieval_context.json`)*

Score, group, title, three enum fields. **No text, no IDs.** The "semantic document retrieval" leg of
the hybrid architecture returns a list of template-generated titles.

### F-03 — CRITICAL: SQL and graph retrieval are completely invariant to the question
```
$ .venv/Scripts/python.exe /tmp/audit/p2_invariance.py
  sql_hash=fdd0e02b6d6b9074  graph_hash=8e511835882c75b2   <- Find sellers with negative customer complaints...
  sql_hash=fdd0e02b6d6b9074  graph_hash=8e511835882c75b2   <- Which seller in Rio de Janeiro sold the most furniture...
  sql_hash=fdd0e02b6d6b9074  graph_hash=8e511835882c75b2   <- purple monkey dishwasher
  sql_hash=fdd0e02b6d6b9074  graph_hash=8e511835882c75b2   <- List the three worst-performing sellers by refund rate
```
Byte-identical evidence for a business question and for `"purple monkey dishwasher"`. No SQL template
(`hybrid_retriever.py:184-288`) and no Cypher template (`hybrid_retriever.py:306-374`) accepts any
parameter except `:limit`. There is no `WHERE` clause bound to anything the user asked.

**The system is not a retrieval system. It is a router that selects one of six pre-baked tables and one
of five pre-baked graph result sets.** Only the Qdrant leg varies with the query, and per F-02 it
returns titles only.

Worse for the flagship question: `seller_performance` is `ORDER BY late_delivery_orders DESC` with no
filter, so it returns the ten **highest-volume** sellers — whose average review scores are 3.83–4.13,
i.e. above the dataset mean. The answer to "which sellers have negative complaints" is grounded on
evidence about sellers with good ratings.

### F-04 — `product_category_name_english` is NULL for all 73 rows
```
$ psql -c "select count(*) filter (where product_category_name_english is null), count(*)
           from ecommerce.product_category_translations;"
 73 | 73
$ head -3 data/processed/translations_cleaned.csv
product_category_name,product_category_name_english
beleza_saude,health_beauty
```
The source CSV has the translations. `FILE_CANDIDATES["product_category_translations"]`
(`postgres_loader.py:54-58`) lists four filenames, none of which is `translations_cleaned.csv`; the glob
fallback (`postgres_loader.py:220-224`) requires the substring `product_category_translations` in the
filename, which also fails. `resolve_dataset_path(required=False)` returns `None`, the loader builds an
empty frame (`postgres_loader.py:357-362`), and the outer merge fills 73 category names from `products`
with English = `None`.

Downstream: all 3,000 tickets have `category_name_english: None`; `vw_product_performance` and the
`category_policy_guide_paths` Cypher template both hand NULL categories to the LLM.

### F-05 — `sentiment_label` is NULL for all 99,224 reviews
`reviews_cleaned.csv` has a column named `sentiment`. `TABLE_COLUMNS["reviews"]` expects
`sentiment_label` (`postgres_loader.py:129`) and `COLUMN_RENAMES` has no entry for reviews
(`postgres_loader.py:133-138`). `standardize_columns` silently creates the missing column as `None`
(`postgres_loader.py:233-235`) with no warning. There is even a B-tree index on the all-NULL column.
`review_intelligence` — one of the six SQL routes, and the one the planner picks for complaint
questions — returns `sentiment_label=null` on every row.

### F-06 — `standardize_columns` silently invents missing columns
`postgres_loader.py:233-235`:
```python
for column in expected_columns:
    if column not in df.columns:
        df[column] = None
```
No log, no warning, no failure. This one construct is the delivery mechanism for both F-04 and F-05 and
will silently swallow any future schema drift.

### F-07 — `/health` is a static literal; proven live
`src/api/main.py:73-79` returns a hardcoded `status="ok"` with no dependency check. During this audit
the API container's DNS failed entirely:
```
$ docker exec enterprise_ai_api python -c "import socket; ..."
  postgres -> FAIL [Errno -3] Temporary failure in name resolution
  neo4j    -> FAIL [Errno -3] Temporary failure in name resolution
  qdrant   -> FAIL [Errno -3] Temporary failure in name resolution
  generativelanguage.googleapis.com -> FAIL [Errno -3] Temporary failure in name resolution
$ docker exec enterprise_ai_api curl -s http://localhost:8000/health
{"status":"ok","service":"enterprise-agentic-workflow-api","generated_at":"2026-08-20T14:45:38Z"}
```
Every dependency unreachable, `/query` returning 500 on 100% of requests, `/health` reporting `ok`.

### F-08 — unauthenticated exception-detail disclosure
`src/api/main.py:118-122` returns `detail=f"Failed to run enterprise workflow: {exc}"` to the caller.
Observed live: `{"detail":"Failed to run enterprise workflow: [Errno -3] Temporary failure in name
resolution"}`. For a DB failure the psycopg2 `OperationalError` text includes host and port — the exact
string is already visible in `reports/observability/workflow_events.jsonl`: `connection to server at
"localhost" (::1), port 5432 failed`. No auth is required to trigger it.

### F-09 — the embedding model is reloaded from disk on every single query
`SentenceTransformer(settings["embedding_model_name"])` is called inside `run_hybrid_retrieval()`
(`hybrid_retriever.py:508`), which runs per request. Visible in the container log as
`Loading weights: 100%|...| 103/103` before every `POST /query`. No module-level cache, no lifespan
hook. Consistent with "no caching", but a straightforward per-request cost.

### F-10 — 9.4% of documents are silently truncated at embedding time
```
model max_seq_length (word-pieces): 256
  logistics_incidents     n= 1000 median=177 max= 197  OVER 256: 0    (0.0%)
  warranty_claims         n= 1000 median=224 max= 248  OVER 256: 0    (0.0%)
  support_tickets         n= 3000 median=176 max= 258  OVER 256: 1    (0.0%)
  customer_emails         n= 3000 median=212 max= 352  OVER 256: 708 (23.6%)
  troubleshooting_guides  n=   73 median=262 max= 326  OVER 256: 54  (74.0%)
  policy_documents        n=   79 median=182 max= 246  OVER 256: 0    (0.0%)
TOTAL silently truncated: 763/8152 (9.4%)
```
The claim "I did not need a traditional long-document chunking strategy" holds for four of six groups.
**74% of troubleshooting guides** — one of the six artifact groups the flagship question routes to —
exceed the window and lose their tail.

### F-11 — requirements.txt has zero version pins
`grep -c "==" requirements.txt` → `0`. Twenty dependencies, all floating, including `langgraph`,
`google-genai`, `sentence-transformers` and `torch`. The image currently resolves to langgraph 1.2.9,
google-genai 2.11.0, sentence-transformers 5.6.0, pandas 3.0.3, numpy 2.5.1, neo4j 6.2.0, torch 2.13.0.
A rebuild next month is a different application. Combined with the missing clean-clone path (P4), this
is the largest reproducibility risk in the repo.

### F-12 — the project's own validators cannot detect F-01 or F-02
- `qdrant_validator.py:210-224` builds the identical compact structure — it **reads**
  `payload.get("seller_id")` and `payload.get("text_preview")` — and never asserts they are non-null.
  It ran during this audit and reported `Overall status: PASS` against a corpus with zero entity IDs.
- `hybrid_retrieval_validator.py:118-140` asserts only `record_count != 0` per source. Five results
  containing nothing but a title pass.
- `synthetic_data_validator.py` reports `Reference checks: PASS` — it validates the JSONL files, where
  the IDs *do* exist. **No validator covers the JSONL → Qdrant boundary, which is where they are lost.**

### F-13 — dead code
- `plan_query_route()` (`agentic_workflow.py:68-97`) is a 30-line keyword-signal planner. It is never
  called; `query_planner_node` calls `plan_query_with_gemini` at line 111. Its name survives only as the
  `operation` string in the observability trace (`agentic_workflow.py:108`), so every event log records
  `operation: "plan_query_route"` for a function that does not run.
- `detect_sql_intent` / `detect_graph_intent` / `detect_vector_filters` (`hybrid_retriever.py:86-179`)
  are unreachable in the API path — the planner always supplies a forced intent.
- `EnterpriseWorkflowState["errors"]` is initialised and never written.
- `ANSWER_PROVIDER=gemini` in `.env` is read by nothing (`grep -rn ANSWER_PROVIDER src/` → no matches).

### F-14 — the API validator does not reproduce today
```
$ docker exec enterprise_ai_api python src/api/api_validator.py --api-base-url http://localhost:8000
RuntimeError: Could not reach the FastAPI server. (HTTP Error 500 on POST /query)
```
Root cause is environmental (F-07: container DNS is dead), not a code defect — the same five cases pass
from the host. But it demonstrates three things at once: `/health` lies, there is no retry or circuit
breaker, and the "complete local Docker runtime test" result is a snapshot from 2026-07-12
(`reports/api_validation_report.json`) that the running deployment no longer satisfies.

Separately, the two "endpoint checks" (`api_validator.py:92-150`) assert that `GET /` returns
`status == "running"` and `GET /health` returns `status == "ok"` — both **hardcoded literals in
`main.py:62-79`**. They cannot fail while the process is alive and they prove nothing about the system.

### F-15 — 73 of 79 policy documents are the same sentence with the category name swapped
`generate_policy_documents()` (`synthetic_data_generator.py:717-790`) emits 6 topic policies plus one
`category_support_policy` per category. All 73 share an identical template body. In a 384-dim cosine
space these are near-duplicates, so retrieving top-5 `policy_documents` returns five interchangeable
boilerplate paragraphs. Observed in a real run: `"Category Support Handling Guide:
industria_comercio_e_negocios"`, `"Category Support Handling Guide: market_place"`, …

Also: policies link to **no seller** (`linked_entities` = `{category_id, category_name_english}` only,
6 of them with both `null`). The flagship question asks for *"sellers associated with … relevant support
policies"* — that association does not exist in the data.

### F-16 — 100% of ticket `customer_message` fields are raw Portuguese Olist review text
`ticket_customer_message()` (`synthetic_data_generator.py:247-297`) returns
`row["review_comment_message"]` verbatim when present; all six English fallback templates are dead code.
```
tickets with English template message: 0 / 3000
tickets with detectable Portuguese:    1858
tickets containing mojibake 'Ã':         70    (e.g. "atÃ©" for "até")
```
Consequences: (a) an English-only MiniLM model embeds Portuguese complaint text; (b) 70 records carry
double-encoded UTF-8 from the load path; (c) the "synthetic" corpus is largely real customer-authored
review text with a synthetic wrapper — worth knowing before calling it synthetic in a privacy context.

### F-17 — real credentials as source-code defaults in 17 places
`os.getenv("POSTGRES_PASSWORD", "enterprise_password")` and
`os.getenv("NEO4J_PASSWORD", "enterprise_neo4j_password")` appear across `postgres_loader.py:282`,
`postgres_validator.py:32`, `sql_view_validator.py:30`, `neo4j_loader.py:383,399`,
`neo4j_validator.py:44`, `synthetic_graph_validator.py:48`, `synthetic_neo4j_loader.py:228`,
`hybrid_retriever.py:30,34`, `synthetic_data_generator.py:67`, `synthetic_data_validator.py:41`. These
are the *actual working* passwords, so a missing env var silently succeeds instead of failing loudly.

**Good news:** `git log --all -- .env` is empty and a regex scan of all four commits for
`AIza…` / `AQ.Ab8RN…` / `sk-…` found nothing. **No secret has ever been committed.** The live Gemini key
exists only in the untracked `.env`, correctly excluded by `.gitignore:16` and `.dockerignore`.

### F-18 — hardcoded `"overall_status": "PASS"` in generated reports
`synthetic_data_generator.py:892` and `hybrid_retriever.py:552` both write `"overall_status": "PASS"` as
a literal into their JSON reports. These files look like validation output and are not. Related:
`agentic_workflow.py:265` defaults the workflow's top-level status to `"PASS"` when the evaluation
summary is missing — the pipeline fails open.

### F-19 — partial failures can escape the observability trace
Two of 31 recorded runs stop after two events (`gemini_planner_agent` span_start + span_end PASS) with
nothing after and no error event. `trace_span` catches and records failures correctly when the exception
happens inside a span (verified: one run has a real `span_error` carrying `OperationalError` detail), but
a crash *between* spans — e.g. the `ValueError` from `validate_and_normalize_plan` on an off-domain
query — leaves a truncated trace with every recorded event marked PASS.

### F-20 — minor
- `agentic_workflow_validator.py:345-352`: duplicated f-string fragment prints
  `Answer chars=3114)Answer chars=3114,` in the summary. Visible in the live run above.
- `seller_ticket_product_paths` uses `ORDER BY t.severity DESC` on a **string** column
  (`hybrid_retriever.py:359`), so ordering is `medium > low > high > critical`.
- `warranty_product_seller_paths` and `customer_ticket_order_product_paths` have **no `ORDER BY` at
  all** — the "top" graph evidence is whatever Neo4j returns first, which is why every run shows
  `WRN-000001 … WRN-000010`.
- `POST /query` creates a directory per distinct query string under `reports/api_runs/`
  (`main.py:83-96`), unauthenticated and unbounded. Path traversal is blocked (`/` and `\` are both
  replaced with `_`), but disk growth is not.
- Qdrant has **no payload index** on `artifact_group` (`payload_schema: {}`) and
  `indexed_vectors_count: 0` (8,152 points is below the 10,000 `indexing_threshold`), so every search is
  a brute-force scan. Fine at this scale; worth knowing it is not exercising HNSW.
- **Compiled bytecode is committed.** `git ls-files | grep -c '\.pyc$'` → **8**. Cause:
  `.gitignore:80` ends with `!src/**`, which negates the earlier `__pycache__/` rule for everything
  under `src/` (`git check-ignore -v` confirms `.gitignore:80:!src/**` is the winning rule). Running any
  validator produces new untracked `.pyc` files that git offers to commit.
- `docker-compose.yml` publishes `8000:8000` for the api service, but the running container reports no
  active host binding and `curl http://127.0.0.1:8000/health` fails from the host while `:8501` works.
  Docker Desktop port-forwarding state, not a repo defect — but the README's "FastAPI runs at
  http://localhost:8000" is not true of the current deployment.

---

# What is genuinely good

Specific, not padded.

1. **The Neo4j validator is real validation.** `neo4j_validator.py:93-208` runs nine orphan checks using
   `NOT EXISTS { ... }` subqueries against every mandatory relationship, plus seven business-path checks
   with counts. I ran it:
   ```
   Overall status: PASS
   orders_without_customer: 0   order_items_without_order: 0   order_items_without_product: 0
   order_items_without_seller: 0   payments_without_order: 0    reviews_without_order: 0
   products_without_category: 0    customers_without_region: 0  sellers_without_region: 0
   negative_review_product_seller_paths: 17756     late_delivery_review_paths: 7701
   ```
   These are non-trivial queries that would genuinely catch a broken load, and the counts reconcile
   against the raw data (`LOCATED_IN = 102,536 = 99,441 customers + 3,095 sellers`). This is the
   strongest engineering in the repository, and it is the *only* validator that could fail for a real
   reason. Contrast F-12.

2. **Every headline number is exact.** Not approximately — exactly. 99,441 / 3,095 / 32,340 / 99,441 /
   111,046 / 103,886 / 99,224 / 73 in PostgreSQL; the same in Neo4j plus the eight claimed relationship
   types; 8,152 Qdrant points at 384 dims; 3,000 / 3,000 / 1,000 / 1,000 / 79 / 73 synthetic files; 6
   views; 8 observability events across exactly the 4 claimed components. I checked all of them and
   found no inflation anywhere. The document is scrupulously honest about magnitudes.

3. **The API's input surface is minimal by design.** `POST /query` takes `{"query": str}` and nothing
   else (`main.py:22-30`). No route override, no limit override, no collection parameter. This is *why*
   my eight planner attacks all failed — the attacker only controls natural language, and the natural
   language is filtered through a fail-closed allowlist. That was a real design decision and it worked.

4. **The allowlist is fail-closed at both layers.** `validate_and_normalize_plan` raises rather than
   defaults (`gemini_query_planner.py:190-231`), and the template dicts `KeyError` rather than fall
   back. Two independent barriers, neither of which silently degrades. Many projects put a
   `.get(x, default)` there; this one did not.

5. **The Gemini evaluator actually discriminates.** I gave it a fabricated answer and a subtly
   number-corrupted answer. It failed both (grounding 1 and 2) and named the specific unsupported
   claims. It is a weak instrument for the reasons in P3, but it is not a rubber stamp, and I expected
   it would be.

6. **Embedding reproducibility is exact.** Same model name from a single env var at ingest and query,
   `normalize_embeddings=True` on both sides, Cosine distance, cos = 1.00000000 on re-encode. This is
   the easiest thing to get subtly wrong in a RAG system, and it is right.

7. **Module boundaries are clean.** Twelve modules, each with a single responsibility, each with a
   paired validator, each independently runnable with `argparse` and a JSON report.
   `retrieval → context → generation → evaluation` genuinely do not reach into each other's internals;
   the answer agent really does only see the built context. The `sys.path.append` shims are ugly (no
   `__init__.py`, no packaging), but the *conceptual* separation is real and would survive being
   packaged properly.

8. **No secret has ever been committed.** `.gitignore` handles `.env` correctly, `.dockerignore`
   excludes it from the image, and four commits of history are clean.

---

# The three findings that would most damage this in a technical interview

### 1. The retrieval layer does not retrieve. (F-03, F-01, F-02)

**Interviewer:** *"Walk me through what happens to my question after the planner. Show me where my
question influences which rows come back."*

**Honest answer today:** It doesn't. The planner picks one of six intent strings; that string indexes a
dict of SQL literals, none of which accepts a parameter other than `LIMIT`. I demonstrated that
`"purple monkey dishwasher"` and the flagship business question return byte-identical SQL and graph
evidence. The only query-dependent leg is Qdrant, and Qdrant returns
`{score, artifact_group, title, severity}` — no document text, because the code reads a payload key
(`text_preview`) that ingest never writes, and no entity IDs, because ingest reads top-level record keys
while every ID lives under `linked_entities`. So the answer agent is being asked to connect a fixed
table of the ten highest-volume sellers to a fixed list of the first ten warranty claims to five
template-generated titles. It produces plausible business prose because Gemini is good at that, not
because the evidence supports it. This is the finding that turns "hybrid retrieval platform" into
"LLM-narrated dashboard" — and it is three separate one-line bugs away from being fixable.

### 2. The five validation cases are the planner's own few-shot examples. (P3)

**Interviewer:** *"Where did the five evaluation questions come from?"*

**Honest answer today:** They are the same five strings hardcoded as worked examples inside the
planner's prompt, and the expected routes are copied from those examples' own "Correct JSON" blocks. The
planner is graded on its answer key, so 5/5 on routing is guaranteed by construction. The three
validators that report "5 cases", "5 cases" and "5 query cases" contain byte-identical case lists, so
the three PASS results are one result reported three times. When I ran the document's own paraphrases —
"Which sellers have the most complaints?" — only 1 of 3 hit the expected routes, and the document's own
worked example, "which seller had the highest revenue," crashes the workflow outright because the plan
schema requires a non-null graph intent. The deterministic half of the pass criterion reduces to
`len(answer) >= 1000`; everything else is Gemini grading Gemini with no human-labelled agreement
measured. The number 5/5 is real and reproducible — I re-ran it and got 5/5 — but it measures that the
pipeline executes, not that it is correct.

### 3. "The runtime workflow is read-only" is false at the database level. (P2)

**Interviewer:** *"You said the runtime is read-only. What is enforcing that?"*

**Honest answer today:** Nothing below the application. `enterprise_user` is a PostgreSQL **superuser**
with `Create role, Create DB, Replication, Bypass RLS`. `records_query` uses `engine.begin()`, a
committing read-write transaction — I passed a row-returning `INSERT` through the project's own helper
and confirmed on a fresh connection that it committed. Neo4j Community has no RBAC and `graph_query`
uses `session.run()`, not `execute_read` — `CREATE`, index DDL and `DELETE` all succeeded with no error.
Qdrant runs with no API key at all, so the runtime client created and dropped a collection, and I
upserted a poisoned document into the live collection with no credentials, watched it rank #1 for the
flagship question, and found its attacker-controlled text verbatim in the context passed to the answer
LLM. The allowlist genuinely held — 8 of 8 adversarial planner prompts were rejected — but it is the
*only* control. The correct claim is "the tool allowlist prevents arbitrary queries," and it should be
said exactly that narrowly, because "read-only runtime" is the kind of claim an interviewer will test
with one `\du`.

---

# Full claim sweep

Verdicts: **CONFIRMED** / **CAVEAT** (confirmed with caveat) / **CONTRADICTED** / **UNVERIFIABLE**.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | 99,441 customers | CONFIRMED | `psql -c "select count(*) from ecommerce.customers"` → 99441 |
| 2 | 3,095 sellers | CONFIRMED | → 3095 |
| 3 | 32,340 products | CONFIRMED | → 32340 |
| 4 | 99,441 orders | CONFIRMED | → 99441 |
| 5 | 111,046 order items | CONFIRMED | → 111046 |
| 6 | 103,886 payments | CONFIRMED | → 103886 |
| 7 | 99,224 reviews | CONFIRMED | → 99224 |
| 8 | 73 product-category translations | CAVEAT | 73 rows, but `product_category_name_english` NULL in 73/73 — F-04. Row count right, content absent |
| 9 | "created the database schema, added indexes" | CONFIRMED | `database/postgres/schema.sql`, `indexes.sql`; 22 indexes live in `pg_indexes` |
| 10 | "created and validated six business views" | CONFIRMED | 6 views in `pg_views`; `views.sql` has 6 `CREATE`; `sql_view_validator.py` exercises them |
| 11 | "validated the database by checking expected row counts and testing the business views" | CONFIRMED | `postgres_validator.py`, `sql_view_validator.py`; both ran PASS |
| 12 | Graph has nine node types | CAVEAT | The nine Olist labels exist; the loaded graph has **15** labels (6 synthetic). Undercount, not overcount |
| 13 | Eight main relationship types | CAVEAT | All 8 present with correct counts; the graph actually has **24** types. Undercount |
| 14 | `Customer PLACED Order` = 99,441 etc. | CONFIRMED | `MATCH ()-[r]->() RETURN type(r), count(*)` — all 8 match the row counts exactly |
| 15 | "created graph constraints and indexes" | CONFIRMED | `SHOW CONSTRAINTS` → 15; `SHOW INDEXES` → 38 |
| 16 | Validation checks node counts, rel counts, missing rels, orphans, business paths | CONFIRMED | `neo4j_validator.py:69-208`; ran it, all present |
| 17 | Validated path `Customer→Order→Payment` | CONFIRMED | `customer_order_payment_paths: 103886` |
| 18 | Validated path `Customer→Order→Review` | CONFIRMED | `customer_order_review_paths: 99224` |
| 19 | Validated `Negative Review→Order→OrderItem→Product→Seller` | CONFIRMED | `negative_review_product_seller_paths: 17756` |
| 20 | "All orphan checks passed with zero missing mandatory relationships" | CONFIRMED | 9/9 orphan checks = 0, live |
| 21 | 3,000 support tickets | CONFIRMED | `wc -l data/synthetic/support_tickets.jsonl` → 3000 |
| 22 | 3,000 customer emails | CONFIRMED | → 3000 |
| 23 | 1,000 logistics incidents | CONFIRMED | → 1000 |
| 24 | 1,000 warranty claims | CONFIRMED | → 1000 |
| 25 | 79 policy documents | CAVEAT | 79 files; 73 are the same template with the category name swapped — F-15 |
| 26 | 73 troubleshooting guides | CONFIRMED | → 73 |
| 27 | 8,152 total synthetic records | CONFIRMED | sum = 8152; Qdrant `points_count: 8152` |
| 28 | Records connected to Olist entities via customer/order/product/seller ID | CAVEAT | True in the JSONL (`build_linked_entities`, generator:299). But emails are an entity-clone of tickets and warranty claims a subset, so 8,152 artifacts cover 3,337 distinct orders — 3.4% (P1) |
| 29 | "allowed vector-search results to be connected back to PostgreSQL and Neo4j evidence" | **CONTRADICTED** | 0 of 8,152 Qdrant points carry customer/order/product/seller ID — F-01 |
| 30 | "a retrieved warranty claim could reference a product ID and seller ID that also existed in PostgreSQL and Neo4j" | **CONTRADICTED** | Warranty-claim payload keys: `artifact_group, artifact_type, claim_id, created_at, document_type, entity_ids{claim_id}, ingested_at, severity, source_file, stable_document_id, text, title`. No product or seller ID |
| 31 | Used `all-MiniLM-L6-v2` | CONFIRMED | `.env` + `qdrant_ingest.py:468`; re-encode cos = 1.0 |
| 32 | Produces 384-dimensional vectors | CONFIRMED | `/collections/enterprise_knowledge` → `"size":384`; `get_embedding_dimension()` → 384 |
| 33 | Collection `enterprise_knowledge` with all 8,152 artifacts indexed | CAVEAT | Collection and count correct; `indexed_vectors_count: 0` — below the 10,000 HNSW threshold, so searches are brute-force |
| 34 | Each record contains embedding, original text, artifact type, record id, entity IDs, metadata | **CONTRADICTED** | Embedding ✓, `text` ✓, `artifact_type` ✓, own record id ✓. **Entity IDs absent** (F-01); `entity_ids` holds only the artifact's own id |
| 35 | Planner could restrict retrieval to specific artifact groups | CONFIRMED | `build_artifact_filter` (`hybrid_retriever.py:392`); per-group filtering observed in live runs |
| 36 | "no traditional chunking needed — records are short" | CAVEAT | True for 4/6 groups; 763/8152 (9.4%) exceed the 256-token window and are silently truncated, incl. 74% of troubleshooting guides — F-10 |
| 37 | PostgreSQL for exact facts, Neo4j for relationships, Qdrant for semantics | CAVEAT | Each source is used for its stated role, but the SQL/graph legs are query-invariant lookups — F-03 |
| 38 | "For complex questions, the system could use two or all three sources" | **CONTRADICTED** | It always uses all three. `run_hybrid_retrieval` (`hybrid_retriever.py:517-548`) calls `sql_retrieve`, `graph_retrieve`, `vector_retrieve` unconditionally; the plan schema requires all three fields |
| 39 | "deterministic retrieval tools instead of unrestricted database commands" | CONFIRMED | Template dicts at `hybrid_retriever.py:184` and `:306`; 8/8 attacks blocked |
| 40 | "LLM could not generate and execute arbitrary SQL, Cypher, Python or shell" | CONFIRMED | P2 attacks 1-2 |
| 41 | "An LLM interprets the user's objective" | CONFIRMED | `plan_query_with_gemini` at `agentic_workflow.py:111` |
| 42 | "It dynamically selects tools and data sources" | **CONTRADICTED** | Selects an *intent within* each source; all three sources always execute — see #38 |
| 43 | "Multiple specialised agents perform different responsibilities" | CONFIRMED | Three distinct Gemini calls with distinct prompts: planner, answerer, evaluator |
| 44 | "LangGraph maintains shared state across the workflow" | CONFIRMED | `StateGraph(EnterpriseWorkflowState)`, `agentic_workflow.py:293-309` |
| 45 | "The final answer is evaluated by another agent" | CONFIRMED | `answer_evaluation_node`, `agentic_workflow.py:213-254` |
| 46 | Workflow order: planner→retrieval→context→answer→eval→response | CAVEAT | Correct, but it is a **strictly linear chain** — no conditional edges, no branches (`agentic_workflow.py:299-307`). LangGraph provides typed state passing over a 5-step function pipeline |
| 47 | Shared state contains query, plan, routes, SQL/graph/vector results, context, answer, evaluation, status | CAVEAT | All present in `EnterpriseWorkflowState:46-61`, except `errors`, which is initialised and never written — F-13 |
| 48 | Planner receives question + source info + allowed SQL/graph/vector routes + output format | CONFIRMED | `build_planner_prompt`, `gemini_query_planner.py:50-172` |
| 49 | "if the question asks for the highest-revenue seller, the planner should select PostgreSQL" | **CONTRADICTED** | Live: `"Which seller had the highest revenue?"` → `ValueError: Invalid graph intent from Gemini: None`. A SQL-only plan is unrepresentable |
| 50 | "The planner output was validated and mapped to predefined deterministic retrieval functions" | CONFIRMED | `validate_and_normalize_plan:190-231` → template dict lookup |
| 51 | Tools return predictable dicts and lists, not free-form text | CONFIRMED | `hybrid_retriever.py:293-300`, `:383-390`, `:485-493` |
| 52 | Context builder normalises, separates sources, groups by entity ID, removes fields, builds evidence | CAVEAT | Normalise/separate/remove ✓ (`retrieval_context_builder.py:114-146`). "Group related evidence using entity IDs" collects IDs from SQL and graph only — the vector leg contributes none (F-01) |
| 53 | "The context-builder validator passed all five representative validation cases" | CAVEAT | The five cases are byte-identical to the workflow validator's and to the planner's own few-shot examples — P3. Not independent evidence |
| 54 | Answer agent receives question + context + grounding instructions | CONFIRMED | `reports/api_runs/.../workflow_prompt.txt` — all instructions present verbatim |
| 55 | "The answer agent does not independently access unrestricted tools" | CONFIRMED | `generate_answer(context, model, key)` — no tool bindings, no function calling |
| 56 | Evaluator checks grounding, completeness, unsupported claims, missing evidence, business usefulness | CONFIRMED | `build_evaluation_prompt:105-119`; output schema at `:120-134` |
| 57 | Evaluator output includes the three scores, unsupported claims, missing evidence, deterministic failures, status | CONFIRMED | `normalize_evaluation:296-318` |
| 58 | "I also used deterministic checks alongside the LLM evaluator" | **CONTRADICTED** in substance | The 5 checks are keyword-presence and length; 4 of 5 are `severity: medium` and cannot cause a FAIL. Only `len >= 1000` is decisive — `gemini_answer_evaluator.py:210-296` |
| 59 | "the workflow stops after evaluation; no automatic replanning" | CONFIRMED | `agentic_workflow.py:299-307` — linear edges only, no conditional edge, no loop |
| 60 | Two endpoints `GET /health` and `POST /query` | CONFIRMED | `main.py:74`, `:82`. (A third undocumented `GET /` exists at `:61`) |
| 61 | "`/health` checks whether the backend and workflow are available" | **CONTRADICTED** | Static literal (`main.py:73-79`); returned `ok` live while all four dependencies were unreachable — F-07 |
| 62 | `/query` accepts an NL question, runs the workflow, returns structured JSON | CONFIRMED | `main.py:82-116`; 200 OK observed in container logs |
| 63 | Response contains status, answer, SQL/graph/vector route summaries, evaluation, report locations | CONFIRMED | `QueryResponse:33-42`; matches `workflow_report.json` |
| 64 | "The API validator passed two endpoint checks and five query cases" | **CONTRADICTED** as a current statement | Was true on 2026-07-12 (`reports/api_validation_report.json`). Today: 2 endpoint checks pass, first query case → HTTP 500 — F-14. The 2 endpoint checks also assert only hardcoded literals, and the 5 query cases are the same 5 |
| 65 | Streamlit shows health, question box, examples, run, answer, routes, artifact groups, eval scores, unsupported claims, missing evidence, reports | CONFIRMED | `streamlit_app.py:61-193, 198-290` — every listed feature present |
| 66 | "Streamlit communicates with FastAPI rather than the databases" | CONFIRMED | Only `requests.get/post` to `API_BASE_URL` (`streamlit_app.py:22-38`); no DB driver imported |
| 67 | Observability writes JSONL events, CSV metrics and a report to `reports/observability` | CONFIRMED | `workflow_events.jsonl` + `workflow_metrics.csv` present; `observability_report.py` exists |
| 68 | "A successful run produced eight events across four traced components" | CONFIRMED | Live: 28 of 31 runs have exactly 8 events across 4 components — planner, hybrid retrieval, answer, evaluation. Exactly as claimed |
| 69 | Logs record operation, start, completion, status, duration, error, run metadata | CAVEAT | All fields present; one run carries a genuine `span_error` with `OperationalError` detail. But crashes *between* spans leave a truncated all-PASS trace — F-19. Also `operation` is `plan_query_route`, a dead function — F-13 |
| 70 | "Token counts were not added to the observability system" | CONFIRMED | No token field in any of 233 events; the only metric name is `duration_ms` |
| 71 | Five Docker services run separately | CONFIRMED | `docker-compose.yml` — postgres, neo4j, qdrant, api, ui; all five containers running |
| 72 | Services communicate via Docker network service names | CONFIRMED | `docker-compose.yml:54-58` sets `POSTGRES_HOST: postgres`, `NEO4J_URI: bolt://neo4j:7687`, `QDRANT_HOST: qdrant`; UI gets `API_BASE_URL: http://api:8000` |
| 73 | "I performed a complete local Docker runtime test; all nine items passed" | CAVEAT | Credible for 2026-07-12 (dated reports exist). Not reproducible today: `/query` 500s in-container — F-14 |
| 74 | "The final agentic workflow passed all five cases" | CAVEAT | Reproduced live today: 5/5 PASS, identical routes and scores. But the five cases are the planner's own few-shot examples — P3 |
| 75 | "I would not describe this as 100% general accuracy" | CONFIRMED | Correct, and understated — 1/3 on the document's own paraphrases (P3) |
| 76 | "I did not create a large benchmark with hundreds of human-labelled questions" | CONFIRMED | No labelled dataset anywhere in the repo |
| 77 | Did not measure precision / recall / hallucination rate / planner accuracy / latency / p95 / tokens / cost / concurrency | CONFIRMED | No such code exists. `duration_ms` is recorded but never aggregated into p50/p95 |
| 78 | "The LLM cannot execute arbitrary shell commands" | CONFIRMED | No `subprocess`, `os.system`, `eval` or `exec` anywhere in `src/` |
| 79 | "The LLM cannot run unrestricted Python" | CONFIRMED | Same |
| 80 | "It does not generate and execute arbitrary SQL or Cypher" | CONFIRMED | P2 attacks 1-2 |
| 81 | "Tool actions are allowlisted" | CONFIRMED | `gemini_query_planner.py:13-38` + fail-closed validation; note the vector-group allowlist exists only at the planner (`hybrid_retriever.py:392` fails open) |
| 82 | "The runtime workflow is intended to be read-only" | **CONTRADICTED** | Superuser account; `engine.begin()` commits; `session.run()` writes; Qdrant unauthenticated — P2 attacks 3-4 |
| 83 | "Credentials are provided through environment configuration" | CAVEAT | True, but the real working passwords are also source-code defaults in 17 places — F-17 |
| 84 | "The project uses structured API requests" | CONFIRMED | Pydantic `QueryRequest`/`QueryResponse`, `main.py:22-46` |
| 85 | No user authentication | CONFIRMED | Zero auth code in `main.py` or `streamlit_app.py`; no `Depends`, no middleware |
| 86 | No role-based authorisation / row-level security / document permissions | CONFIRMED | None exists; `Bypass RLS` on the account makes RLS moot anyway |
| 87 | No managed secret storage | CONFIRMED | Plain `.env` + `python-dotenv` |
| 88 | No prompt-injection protection | CONFIRMED | Demonstrated live — P2 attack 5 |
| 89 | No security-grade audit logs | CONFIRMED | Observability logs operations, not principals; there are no principals |
| 90 | "Because Gemini is external, query and evidence may be sent to Gemini" | CONFIRMED | The full context is embedded in the prompt (`workflow_prompt.txt`, 6,954 bytes incl. real customer IDs and Portuguese review text) and sent to `generativelanguage.googleapis.com` three times per query |
| 91 | Only five end-to-end validation questions | CONFIRMED | And they are the planner's own examples — P3 |
| 92 | "Synthetic support and operational documents" | CAVEAT | The *scaffolding* is synthetic; 100% of ticket `customer_message` values are verbatim real Olist review text — F-16 |
| 93 | No human-calibrated evaluation dataset | CONFIRMED | None exists |
| 94 | No single-agent baseline comparison | CONFIRMED | None exists |
| 95 | No automatic replanning / answer-regeneration loop | CONFIRMED | Linear graph, `agentic_workflow.py:299-307` |
| 96 | No retrieval reranker | CONFIRMED | Raw cosine order straight from Qdrant, no rerank step |
| 97 | No caching | CONFIRMED | No cache anywhere — and the embedding model is reloaded from disk on every request, F-09 |
| 98 | No automatic retries or circuit breakers | CONFIRMED | Demonstrated: one DNS failure → HTTP 500, no retry — F-14 |
| 99 | No load testing | CONFIRMED | No such code |
| 100 | No token or cost calculation | CONFIRMED | See #70 |
| 101 | No CI/CD | CONFIRMED | No `.github/`, no test files, no pytest, no packaging |
| 102 | No cloud deployment | CONFIRMED | Local compose only |
| 103 | No backup / DR / HA | CONFIRMED | Single-replica containers, named volumes, no backup config |
| 104 | "production-oriented, fully validated local prototype" | **CONTRADICTED** on "fully validated" | The retrieval layer is untested by anything (F-12), the five cases test the planner on its own answer key, and the only decisive deterministic gate is a string-length check |
| 105 | "I prepared the databases, loaded and validated the data, created the graph, generated synthetic artifacts, built the vector index, implemented hybrid retrieval, integrated Gemini, created the LangGraph workflow, exposed it through FastAPI, developed the Streamlit interface, added observability, containerised the platform, ran the validators" | UNVERIFIABLE | All the artefacts exist and are consistent with this. Four commits, all authored by `LomadaDharmaTeja`, but three of the four are bulk commits, so the repo carries no evidence of incremental authorship either way. Not something an audit can settle |
| 106 | "I used AI-assisted development … but I executed the commands, analysed the errors, made the design decisions" | UNVERIFIABLE | No trace in the repo either way |
| 107 | The Olist dataset is the core structured dataset | CONFIRMED | `data/raw/olist_*.csv`, Oct 2021 file dates, counts match the public dataset |
| 108 | The dataset contains customers, sellers, products, categories, orders, items, payments, reviews | CONFIRMED | 8 tables in the `ecommerce` schema |
| 109 | (Not claimed) geolocation data | UNVERIFIABLE / not claimed | `geolocation_cleaned.csv` (44 MB) exists on disk but is loaded into neither PostgreSQL nor Neo4j. Unused artefact; the document does not claim it, so no contradiction |
| 110 | "Refining the routing logic, improving planner instructions, restricting the planner, repeatedly running the same validation cases" fixed early routing failures | CAVEAT | The mechanism is visible: eight hand-written routing rules (`gemini_query_planner.py:92-100`) and five worked examples were added to the prompt. But "repeatedly running the same validation cases" until they pass, where those cases *are* the prompt examples, is overfitting rather than fixing — P3 |

---

## Appendix — audit hygiene

All scripts are in `/tmp/audit/`. Nothing in `src/`, `database/`, `configs/`, `docs/` or `README.md` was
modified; `AUDIT.md` is the only file created. Three reversible write probes were performed against the
local dev stack and cleaned up, each verified back to its original state:

| Probe | Target | Cleanup verified |
|---|---|---|
| `INSERT … RETURNING` + `DELETE … RETURNING` | `ecommerce.product_category_translations` | count 73 → 74 → 73 |
| `CREATE`/`DELETE` node, `CREATE`/`DROP INDEX` | Neo4j `:AuditPwnProbe` | 1 node created, 1 deleted; index dropped |
| `upsert` + `delete` poisoned point; create/delete scratch collection | Qdrant `enterprise_knowledge` | 8152 → 8153 → 8152 |

Live Gemini calls made during the audit: ~25 (8 planner attacks, 8 generalisation probes,
5 workflow cases × 3 agents, 3 evaluator probes).
