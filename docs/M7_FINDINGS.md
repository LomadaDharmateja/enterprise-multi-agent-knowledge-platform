# M7 — Observability

The milestone in one line: the trace stopped being a thing the code *asserts* about
itself and became a thing that is *measured*.

The exit criterion is "a failing run is diagnosable from the trace alone, without
re-running it." Four things had to be true for that, and each is a number or a test
below: spans have to carry real durations, a crash has to reach the trace, a recorded
run has to be replayable, and the trace has to be reachable.

Test count: **238 → 244**. All 244 pass.

---

## 1. The defect this milestone exists to fix

`src/observability/observability.py::trace_span` initialised every span with

```python
span_data["status"] = "PASS"
```

and downgraded it only if an exception propagated through that specific context
manager. Three consequences, all of them silent:

| Failure | What the trace said |
|---|---|
| Crash inside an instrumented block | correctly `FAIL` |
| Crash in a LangGraph edge, between two spans | every span `PASS`, no record of the failure |
| A span whose `end` never ran | `PASS`, indistinguishable from a completed one |

The second row is the one the rebuild plan names. A run could fail and leave a trace
in which every recorded event was marked PASS.

OpenTelemetry inverts the default. A span's status is `UNSET` until something sets it;
`OK` is reached only by a clean exit through the context manager; a failure is
`record_exception` plus `set_status(ERROR)`; and an unended span is visibly unended
rather than quietly successful. `test_no_span_defaults_to_a_passing_status` asserts the
inversion directly — a freshly created span is `UNSET`, not `OK`.

---

## 2. Spans with real durations

The first instrumentation pass reconstructed the three retrieval leg spans *after*
`run_hybrid_retrieval` returned, from the result dictionaries. Every leg reported
`duration_ms = 0.0`. The spans were a description of the work, not a measurement of it.

The fix threads the tracer into the retriever: `retrieval_parent_span` brackets the
whole retrieval unit and one `retrieval_span` brackets each leg, opened before the
database call and closed after it.

Hierarchy, from a live run (`agentic_workflow_20260902T205350_c3932ef8`):

```
workflow                OK   20491.76 ms   overall_status=PASS
├─ agent.planner        OK    6148.99 ms   2930 in / 146 out  $0.00095
│                                          sql_intent=seller_performance
│                                          graph_intent=seller_ticket_product_paths
├─ retrieval            OK    1136.75 ms
│  ├─ retrieval.vector  OK     250.76 ms   record_count=5
│  │                                       artifact_groups=['support_tickets']
│  ├─ retrieval.sql     OK     733.31 ms   record_count=10
│  │                                       intent=seller_performance
│  │                                       sort_by=late_delivery_rate
│  └─ retrieval.graph   OK     150.43 ms   record_count=10
│                                          intent=seller_ticket_product_paths
│                                          evidence_linked_filtering_applied=True
├─ agent.answer         OK    8358.08 ms   3438 in /  892 out  $0.00220
└─ agent.evaluator      OK    4585.26 ms   9225 in /  249 out  $0.00268
                                           grounding_score=5
```

Cost and tokens are span **attributes**, not a separate report, which is what the
rebuild plan asked for. `test_retrieval_spans_record_evidence_linking` now asserts
`duration_ms > 0` on a leg that really ran, so the reconstruction cannot come back.

### Two bugs the fix introduced, found by reading output rather than trusting tests

Both were invisible to the test suite and visible in one trace dump:

1. **`annotate_leg_span` landed inside the evidence-link fallback branch** for the SQL
   and graph legs. Attributes were attached only when a fallback fired. The symptom was
   `retrieval.sql` and `retrieval.graph` carrying nothing but `retrieval_leg` while
   `retrieval.vector` was fully annotated.
2. **The span closed before the fallback re-query**, so a fallback's second database
   call happened outside the span that was supposed to be timing it.

Both were fixed by rewriting the leg blocks so one span brackets all the work for that
leg and annotation is unconditional and inside the span — the attributes then describe
the leg that actually ran, fallback included.

---

## 3. Crash propagation — four tests, and what each proves

All four are in `tests/test_otel_spans.py`.

| Test | What it proves |
|---|---|
| **`test_a_crash_between_spans_still_fails_the_root_span`** | **The rebuild-plan requirement.** A `KeyError("planned_route")` is raised with *no agent span open*. Asserts `agent.planner` stays `OK`, `workflow` becomes `ERROR`, and the root's description names the `KeyError`. This is exactly the gap the old tracer could not see. |
| `test_a_crashed_workflow_produces_an_error_span_not_an_ok_one` | A failure *inside* a span marks that span and the root `ERROR`, while the sibling that genuinely succeeded stays `OK`. Failure propagates up without being smeared sideways. |
| `test_the_error_span_carries_the_exception` | The exception type and message are recoverable from the span's status description and its `exception` event. This is the "diagnosable from the trace alone" criterion made concrete. |
| `test_no_span_defaults_to_a_passing_status` | A span is born `UNSET`, never `OK`. The root cause of the original defect, asserted at the source. |

---

## 4. Replay verification

The JSONL event log **survived the migration on purpose**. OTel carries the trace; the
JSONL run file is the replay input, and `test_the_jsonl_event_log_is_still_written`
guards it. `scripts/replay_run.py` reads it.

### Correlation, checked offline over the whole M3 run

`find_recorded_run` matches an eval item to the workflow run that produced it by exact
query text inside the eval run's wall-clock window, then **refuses the correlation
unless the event log and the eval report agree on the route** — two independent records
of the same run.

**82 of 82 items** of `run_20260825T145424Z_full2` correlate, to 82 distinct run ids.
`test_every_recorded_m3_run_is_identifiable_from_the_event_log` asserts this, and needs
no API key and no database.

### The replay

| | |
|---|---|
| Eval run | `run_20260825T145424Z_full2` (the M3 headline 82-item run) |
| Item | `C33` — multi-hop, routes all three legs |
| Question | *For the sellers with the worst late-delivery rates, what do their customers actually complain about?* |
| **run_id replayed** | **`agentic_workflow_20260825T150209_69adc391`** |
| Recorded at | 2026-08-25T15:02:13Z |

| Field | Original route | Replayed route | |
|---|---|---|---|
| `sql_intent` | `seller_performance` | `seller_performance` | match (asserted) |
| `graph_intent` | `seller_ticket_product_paths` | `seller_ticket_product_paths` | match (asserted) |
| `vector_artifact_groups` | `['support_tickets']` | `['support_tickets']` | match (reported) |
| `sql_sort_by` | `late_delivery_rate` | `late_delivery_rate` | match (reported) |

Repeated three times: the asserted fields matched **3/3**. `vector_artifact_groups`
diverged on 1 of the 3 (`['support_tickets', 'customer_emails']`), which is why it is
reported and not asserted.

`test_replaying_a_recorded_run_reproduces_its_route` asserts the two route fields. It
is marked `requires_gemini` because no temperature is pinned — the planner is an LLM and
this measures agreement, it does not assume determinism.

### Stability across items — the finding worth keeping

Seven items × 2 replays = **14 replays**:

| Field | Matched |
|---|---|
| `sql_intent` | **14/14** |
| `graph_intent` | **10/14** |
| `vector_artifact_groups` | 14/14 |
| `sql_sort_by` | 14/14 |

The four `graph_intent` divergences are two items, each diverging **2/2** — stable, so
this is not sampling noise. It is a behaviour change between M3 and M7, and in both
cases the current system routes *better* than the recording:

| Item | M3 recorded | Replayed now | Eval set expects |
|---|---|---|---|
| `C37` "Which product categories generate both high complaint volume and high sales volume?" | `category_policy_guide_paths` | `customer_ticket_order_product_paths` | **required:** `customer_ticket_order_product_paths` |
| `E75` "Which sellers are bad?" (adversarial) | `seller_ticket_product_paths` | `None` | **required:** `None`, **permitted:** `[]` |

Both recordings were route violations against the eval set; both replays comply. So the
honest headline is not "replay is deterministic." It is: **the asserted route fields
reproduce, `sql_intent` perfectly, and where `graph_intent` does not reproduce it is
because M4–M6 fixed the routing, not because the planner is noisy.** Recorded runs are
usable as regression fixtures on `sql_intent`; on `graph_intent` they are a snapshot of
M3 behaviour and two of them are now knowingly stale.

Reports: `tests/eval/results/m7_replay.json`, `tests/eval/results/m7_replay_sweep.json`.

---

## 5. The trace endpoint

`GET /traces/{run_id}` returns the spans for one run as JSON — `name`, `status`,
`attributes`, `duration_ms`, `parent_span_id`, plus `span_id`/`trace_id`/`events` — read
from the in-memory span store the OTel provider exports to alongside console or OTLP.

Decisions, and why:

- **Bearer auth, same gate as `/query`.** A trace carries the query text and the
  answer's attributes. `test_the_trace_endpoint_requires_authentication` asserts 401
  for a missing and for a wrong token.
- **404, not an empty list, for an unknown run.** `{"spans": []}` reads as "this run
  produced no spans", which is a different and much worse claim than "this run is not
  held here." The store is bounded at 50 runs and the 404 body says so.
- **`run_id` added to the `POST /query` response.** Without it a caller cannot ask for
  the trace of the request it just made.

Live, end to end (`POST /query` → `run_id` → `GET /traces/{run_id}`):

```
POST /query                 -> 200   run_id=agentic_workflow_20260902T205350_c3932ef8
GET  /traces/{run_id}       -> 200   span_count=8  status=OK  duration_ms=20491.762
                                     trace_id=35c248844fd238143a8fae27b3e768de
GET  /traces/{run_id}       -> 401   (no Authorization header)
GET  /traces/no_such_run    -> 404
```

The eight spans are the tree in §2. Full response:
`tests/eval/results/m7_trace_endpoint_response.json`.

`test_the_trace_endpoint_returns_the_spans_for_a_run` asserts ≥ 6 spans, the four
required fields on every span, exactly one root, and that each `retrieval.*` leg's
parent is the `retrieval` span.

### A defect found only by looking at the live response

The first version ordered spans by `start_time`. Spans arrive in *completion* order,
which puts a parent after every one of its children, so sorting was necessary. It was
not sufficient:

```
retrieval.vector     OK     374.90 ms  parent=735f54ea6b7ecaeb
retrieval            OK    4480.93 ms  parent=1613ebb95ff3671b   <- 735f54ea6b7ecaeb
```

`retrieval.vector` was listed **above its own parent**. `time.time_ns()` resolves to
about **0.36 ms** on this Windows host (measured: 292 distinct values in 300 ms), and a
parent and the child opened immediately inside it land on the same tick — identical
`start_time` to the nanosecond. Python's stable sort then fell back to insertion order,
which is completion order, and the child finished first.

Fixed by sorting on `(start_time, depth-in-tree)`, so a parent precedes its children
regardless of clock resolution.
`test_a_parent_span_is_never_listed_below_its_own_child` asserts it.

This is a small bug with a general lesson for this project: the tests passed on the
broken ordering because none of them asserted order. It was found by reading one real
response — the same way both §2 bugs were found.

---

## 6. What is exported, and what is not kept

`OTEL_EXPORTER` selects the exporter:

| Value | Behaviour |
|---|---|
| `console` (default) | human-readable spans on stdout, plus the memory store |
| `otlp` | OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT`, plus the memory store |
| `memory` | memory store only — tests and `/traces` |
| `none` | memory store only, nothing emitted |

The memory store always runs so `/traces` works with no infrastructure. It is a
**viewer, not a backend**: bounded at 50 runs, in the API process, lost on restart. For
retention, point `OTEL_EXPORTER=otlp` at a collector. Stating that here because "we
have distributed tracing" would be the kind of claim this rebuild exists to stop making.

Dependencies pinned in `requirements.txt`: `opentelemetry-api`, `opentelemetry-sdk`,
`opentelemetry-exporter-otlp-proto-http`, all `==1.44.0`.

---

## 7. Limitations

- **Replay is not deterministic by construction.** No temperature is pinned on the
  planner. §4 measures agreement rather than asserting determinism, and reports the
  4/14 `graph_intent` divergence rather than averaging it away.
- **Two recorded routes are knowingly stale** (`C37`, `E75`). They are M3 snapshots of
  behaviour that M4–M6 corrected.
- **Traces do not survive a restart** under the default exporter. See §6.
- **Replay re-plans; it does not re-execute the recording.** M8's demo mode needs the
  stronger form — replaying a stored run with no API key at all — and that is not built
  here.
- **The JSONL log grows without bound.** 2,190 events across 314 runs today. It is the
  replay input, so it is not rotated yet; that is a deliberate deferral, not an
  oversight.
