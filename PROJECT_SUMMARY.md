# What I'm building — project summary

*Enterprise Knowledge Intelligence Platform. Status: v1 built and audited; v2 rebuild in progress.*

---

## 1. In one paragraph

A business user asks a question in plain language — *"which sellers are associated
with negative complaints, warranty issues, and the support policies that apply to
them?"* — and gets back a grounded answer with the evidence behind every claim.
Answering that question properly requires three different kinds of data that live
in three different stores: transactional facts, entity relationships, and free
text. The system plans which sources a question needs, retrieves from each,
assembles the evidence, writes the answer, and checks that answer against the
evidence before returning it.

---

## 2. The problem

**Enterprise information fragmentation.** In a real organisation the information
needed to answer one business question is almost never in one place.

Take the seller-complaints question. To answer it honestly you need:

| What you need | Where it lives | Why that store |
|---|---|---|
| Seller order volume, revenue, review averages, late-delivery rates | PostgreSQL | exact aggregation, ranking, joins — vector search cannot count |
| How a complaint connects to an order, a product, and a seller | Neo4j | multi-hop traversal; the same query in SQL is four self-joins |
| The actual complaint text, warranty claims, applicable policies | Qdrant | semantic similarity over unstructured text |

Today an analyst answers this by hand: write SQL, write Cypher, search the ticket
system, then reconcile three result sets by eye. It takes an afternoon, it isn't
repeatable, and the reconciliation step is where mistakes enter.

**Why a plain chatbot cannot do this.** It has no access to operational data.
**Why vector-only RAG cannot do this.** Embeddings are good at "find text like
this" and bad at "count these," "rank those," and "traverse from here to there."
Asking a vector store for the top-5 sellers by late-delivery rate returns
documents that *talk about* late deliveries, not the answer.

---

## 3. Why an agent, specifically

A fixed keyword router works when everyone phrases questions the same way. They
don't. These three mean the same thing:

- "Which sellers have the most complaints?"
- "Find vendors connected to poor customer experiences."
- "Show suppliers associated with negative reviews and warranty issues."

An LLM planner interprets intent and produces a structured retrieval plan —
which sources, which routes, which filters. That plan is then validated against
an allowlist and mapped to parameterised query templates. The LLM decides *what
to ask for*; it never writes the query itself.

**Being honest about the boundary:** this is a read-and-report system, not one
that takes actions with consequences. That's a real limit on how "agentic" it is,
and I'd rather state it than oversell it. The judgment the agent exercises is
source selection and evidence assembly, and that judgment is measurable — which
is what v2 is built around.

---

## 4. Architecture

```
Question
  → Planner agent          (intent → structured plan; allowlist-validated)
  → Hybrid retrieval       (PostgreSQL / Neo4j / Qdrant, parameterised templates)
  → Context builder        (normalise three result shapes; group by entity ID)
  → Answer agent           (evidence-only; no tool access)
  → Evaluator agent        (grounding, completeness, unsupported claims)
  → Replan loop            (one retry if evidence is inadequate)
  → Response + evidence trail
```

Orchestrated with LangGraph over a shared state object. Exposed through FastAPI.
Postgres, Neo4j, Qdrant, API and UI run as five Docker services.

**Data:** the Olist Brazilian e-commerce dataset — 99,441 customers, 3,095
sellers, 32,340 products, 99,441 orders, 111,046 order items, 103,886 payments,
99,224 reviews — loaded into PostgreSQL and mirrored into Neo4j as 9 node types
and 8 relationship types. On top of that, a synthetic enterprise corpus (support
tickets, emails, logistics incidents, warranty claims, policies, troubleshooting
guides) linked to real Olist entity IDs and embedded into Qdrant.

---

## 5. What I got wrong in v1

I built the system, wrote a careful description of it, and then had it
independently audited against that description. 110 claims tested: 78 confirmed,
18 confirmed with caveats, 11 contradicted. The row counts and the graph were all
exact. The retrieval layer was not.

### The defects that mattered

**The retrieval layer did not retrieve.** No SQL or Cypher template accepted any
parameter except `LIMIT`. The audit demonstrated that the flagship business
question and the string `"purple monkey dishwasher"` returned byte-identical
evidence. The planner was choosing between six pre-baked tables and five pre-baked
graph result sets. Worse, the seller route sorted by raw late-delivery *count*
with no filter, so a question about sellers with complaints was answered using the
ten highest-volume sellers — whose average review scores are above the dataset
mean.

**The vector layer could not join to anything.** Zero of 8,152 Qdrant points
carried a customer, order, product or seller ID. Ingest read those keys at the top
level of each record while every ID actually lived one level down. The entire
stated architectural thesis — vector hits joining back to SQL and graph evidence —
was unimplemented.

**No document text ever reached the model.** The retriever read a payload field
called `text_preview`; ingest wrote one called `text`. The semantic-retrieval leg
was returning a score, a group name, and a template-generated title. Nothing else.

**"Read-only runtime" was false.** The database account was a PostgreSQL
superuser. The SQL helper used a committing transaction. Neo4j used a
write-capable session. Qdrant ran with no API key at all — the auditor upserted a
poisoned document with no credentials, watched it rank first for the flagship
question, and found its text verbatim in the prompt sent to the answer model.

**The evaluation was circular.** My five test cases were byte-identical to the
five worked examples embedded in the planner's own prompt, and the expected routes
were copied from those examples' answer keys. The planner was graded on its own
answer sheet. Three separate validators reported "5 cases passed" using the same
five cases. The deterministic half of the pass criterion reduced, on inspection,
to *is the answer at least 1,000 characters long*. Run on paraphrases instead, the
planner got 1 of 3 right, and my own documented example — "which seller had the
highest revenue?" — crashed the workflow, because a SQL-only plan was
unrepresentable in the schema.

**Silent data corruption.** `sentiment_label` was NULL for all 99,224 reviews and
`product_category_name_english` NULL for all 73 categories, because the loader
silently invents any column it can't find instead of failing.

### The process mistakes behind them — the more useful lesson

1. **I described the system from memory instead of from the code.** Everything I
   wrote was what I intended to build. Some of it wasn't what ran. Those are
   different documents and only one can be checked.
2. **I validated each stage in isolation.** Twelve validators, all passing, while
   the system was inert — because the bug lived in the *handoff* from JSONL to
   Qdrant, and no validator looked at a seam.
3. **I tuned until the tests passed, where the tests were the prompt's own
   examples.** That is overfitting, not fixing.
4. **My reports could not fail.** Two of them hardcode `"overall_status": "PASS"`
   as a literal. `/health` returned `ok` while all four dependencies were
   unreachable.

### What genuinely held up

Worth stating, because it's real: the Neo4j validator runs nine orphan checks and
seven business-path checks that could actually fail. Every headline number was
exact — no inflation anywhere. Embedding reproducibility is bit-exact
(cosine = 1.00000000 on re-encode), which is the easiest thing to get subtly wrong
in a system like this. The tool allowlist is fail-closed at two independent layers
and rejected 8 of 8 adversarial planner prompts, including one where the model was
successfully tricked into emitting an out-of-allowlist route. The API's input
surface is one string, by design, which is *why* those attacks failed. And no
secret was ever committed.

---

## 6. What I'm fixing

**Data foundation.** The synthetic corpus is being regenerated. In v1 it was a
deterministic `ORDER BY ... LIMIT` over a PostgreSQL view, which means every
ticket sat on `review_score = 1, is_late_delivery = True` — zero variance — and
the vector layer contained no information the SQL layer didn't already have. It
was a mirror, not a second source. The new corpus carries what structured tables
can't express: root causes, resolution paths, escalation history, policy
exceptions. Severity varies. Some complaints land on well-rated sellers. Only then
does hybrid retrieval have anything to discover.

**Retrieval.** Templates become parameterised — the planner extracts entities and
filters and binds them as values, while the allowlist continues to constrain the
*shape*. SQL-only and graph-only plans become representable. Entity IDs and
document text are carried correctly into the Qdrant payload and back out.

**Loader.** Missing columns fail loudly instead of being invented.

**Security.** A non-superuser read-only database role, read-only transactions,
`execute_read` for Neo4j, an API key on Qdrant, authentication on the API,
sanitised error responses, and a `/health` that actually checks its dependencies.

---

## 7. What I'm adding

**Evaluation, which is the centrepiece.** A held-out set of 60–100 questions that
are *not* the planner's examples, each labelled with expected routes, expected
source records, and a reference answer. Metrics: planner routing accuracy,
retrieval precision and recall@k, groundedness, hallucination rate, and refusal
correctness on questions with no valid answer. The LLM judge gets calibrated
against blind human labels with Cohen's kappa reported — including if it falls
below threshold.

**A single-agent baseline.** One prompt, three databases, no planner. If the
multi-agent pipeline doesn't beat it, that's a finding worth publishing. Right now
nobody knows which is better, which means the architecture is currently
unjustified either way.

**Reliability.** Evaluator-triggered replanning capped at one retry — and measured,
so I can say what fraction of failures it recovers and at what added cost. Retries
with backoff, circuit breakers, timeouts, and graceful degradation: if Neo4j is
down, answer from the other two and say so, rather than returning a 500.

**Cost and performance.** Token accounting per agent call, cost per query, p50/p95
latency per stage, caching with a measured hit rate and spend reduction, and a
concurrency test. A reranker only if the evaluation shows retrieval precision is
actually the bottleneck.

**Observability.** OpenTelemetry spans with cost and tokens as attributes, traces
that can't silently truncate, and deterministic replay of a recorded run.

**Delivery.** Contract tests at every stage boundary, CI that demonstrably fails
when a test fails, pinned dependencies, a reproducible clean clone, cloud
deployment with a public URL, a demo mode requiring no API key, and a UI that
shows the evidence behind every claim.

**A second audit at the end**, run the same way, with the verdict table published.
v1: 11 contradicted out of 110. Target: zero, with every remaining limitation
stated in the README itself.

---

## 8. What I'll be able to say when it's done — and what I won't

**Will be able to say:** natural-language questions produce different, correct
retrieval plans across a held-out set, at a stated accuracy with a confidence
interval; answers are grounded in evidence that is shown to the user; the system
degrades gracefully and refuses when evidence is insufficient; cost and latency
are known per query; and the multi-agent design either beats a single-agent
baseline by a measured margin or doesn't, stated either way.

**Won't be able to say:** that the enterprise corpus is real (it's synthetic, and
the underlying complaint text is real Portuguese customer review data, which has
its own implications worth naming); that the system is production-hardened for
multi-tenant use; or that it takes actions rather than reporting.

The point of the rebuild isn't a system with no limitations. It's a system where
every claim I make about it is true and every number is reproducible.
