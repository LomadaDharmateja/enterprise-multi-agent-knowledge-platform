# Setup: from `git clone` to a working stack

**Verified on 2026-08-24 at commit `9678078`.** This document was written by cloning
the repository into an empty directory (`C:\enterprise_ai_cleanclone`), building a
fresh venv and a fresh set of Docker volumes, and following it end to end. Every step
below was executed; every number below was measured on that run. The final check was
a live `POST /query` returning HTTP 200 with a grounded 3,427-character answer.

Before M0, following `README.md` section 20 from a clean clone was impossible. Four
things were missing:

| Missing | Why it mattered |
|---|---|
| The Olist download | Never mentioned in `README.md` or `docs/` |
| The data-cleaning stage | Lived only in `notebooks/01_olist_exploration.ipynb`, matched by the `notebooks/` rule in `.gitignore`. Now `scripts/clean_data.py` |
| `src/synthetic/synthetic_data_generator.py` | Never mentioned anywhere, yet `synthetic_neo4j_loader.py` and `qdrant_ingest.py` both require its output. Following README §20 verbatim fails at "Synthetic Neo4j Graph" |
| `database/postgres/schema.sql` | Committed but never invoked by any documented command |

## What the clean-clone run measured

| Step | Wall clock | Result |
|---|---|---|
| `pip install -r requirements.txt` | 4m59s | All 20 pins resolved |
| `pip install -e ".[test]"` + `pytest -m "not requires_stack"` | 12s | 54 passed |
| `scripts/clean_data.py` | 9.5s | 9 CSVs, **byte-identical** (sha256) to the originals |
| `schema.sql` | 2s | 8 tables, 8 PKs, 7 FKs, 16 CHECKs |
| `postgres_loader.py` | 1m09s | 8 tables, row counts exact |
| `views.sql` + `indexes.sql` | 3s | 6 views, 22 indexes |
| `neo4j_loader.py` | 1m45s | 9 node labels, 8 relationship types, 552,961 nodes |
| `synthetic_data_generator.py` | 4s | 8,152 records |
| `synthetic_neo4j_loader.py` | 8s | 16 relationship types |
| `qdrant_ingest.py` | 4m45s | 8,152 points (includes the model download) |
| `docker compose build api ui` | ~9m30s | api 9.64 GB, ui 9.21 GB |
| `POST /query` | 22.5s | HTTP 200, evaluator PASS, grounding 5 |
| `pytest` (full, with stack) | 18s | **70 passed** |

Roughly 25 minutes of machine time, most of it `pip` and the image build.

## 0. Prerequisites

| Requirement | Version used | Notes |
|---|---|---|
| Python | 3.12.2 | 3.12.x. `requirements.txt` is pinned against this |
| Docker Desktop | with Compose v2 | Five services; Neo4j needs ~2 GB |
| Git | any | |
| Disk | ~25 GB | The api and ui images are 9.64 GB and 9.21 GB (see below), plus ~120 MB raw data, ~1 GB Postgres and the model cache |
| Kaggle account | free | The Olist dataset is not redistributed in this repository |
| Google Gemini API key | | `GEMINI_API_KEY`; three agents call it per query |

The stack listens on 5432, 7474, 7687, 6333, 6334, 8000 and 8501. **Free them first** --
compose fails with `Bind for 127.0.0.1:8000 failed: port is already allocated` and
leaves the other services running, which looks like a partial success.

> The 9 GB images are not a mistake in the pin set. `sentence-transformers` pulls
> `torch`, and the default PyPI wheel for Linux is the CUDA build -- 423 MB of
> cuBLAS, 206 MB of cuDNN, 60 MB of nvshmem -- into a container that only ever runs
> the embedding model on CPU. The images were already this size before M0 with
> unpinned dependencies. A CPU-only wheel index belongs in the multi-stage
> Dockerfile work in M8.

---

## 1. Clone and create the environment

```powershell
git clone https://github.com/LomadaDharmateja/enterprise-multi-agent-knowledge-platform.git
cd enterprise-multi-agent-knowledge-platform

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e ".[test]"
```

`requirements.txt` is fully pinned. `pip install -e ".[test]"` adds pytest and makes
`pytest` runnable from the repository root.

Sanity check — these need no database and no API key:

```powershell
pytest -m "not requires_stack"
```

---

## 2. Configure the environment file

```powershell
Copy-Item .env.example .env
```

Then edit `.env` and set `GEMINI_API_KEY`. Everything else works as shipped for a
local Docker stack.

`.env` is gitignored and must never be committed. `.env.example` carries placeholders
only.

> The database passwords in `.env.example` are also hardcoded as source-code defaults
> in 17 places (`os.getenv("POSTGRES_PASSWORD", "enterprise_password")` and similar).
> That is a known finding, scheduled for M6. It means the stack starts even with a
> partial `.env`, which hides configuration errors.

---

## 3. Download the Olist dataset

The raw data is not committed (`data/raw/*` is gitignored, ~120 MB).

1. Sign in to Kaggle and open
   <https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce>
2. Download the archive and unzip it into `data/raw/`.

`data/raw/` must then contain exactly these nine files:

```
olist_customers_dataset.csv
olist_geolocation_dataset.csv
olist_order_items_dataset.csv
olist_order_payments_dataset.csv
olist_order_reviews_dataset.csv
olist_orders_dataset.csv
olist_products_dataset.csv
olist_sellers_dataset.csv
product_category_name_translation.csv
```

`scripts/clean_data.py` names any that are missing and exits.

---

## 4. Clean the raw data

```powershell
python scripts/clean_data.py
```

Writes nine `*_cleaned.csv` files into `data/processed/`. Expected output:

```
customers_cleaned.csv          99441 ->   99441 rows (     0 dropped)
orders_cleaned.csv             99441 ->   99441 rows (     0 dropped)
order_items_cleaned.csv       112650 ->  111046 rows (  1604 dropped)
products_cleaned.csv           32951 ->   32340 rows (   611 dropped)
payments_cleaned.csv          103886 ->  103886 rows (     0 dropped)
reviews_cleaned.csv            99224 ->   99224 rows (     0 dropped)
sellers_cleaned.csv             3095 ->    3095 rows (     0 dropped)
geolocation_cleaned.csv      1000163 ->  738332 rows (261831 dropped)
translations_cleaned.csv          71 ->      71 rows (     0 dropped)
```

Two known data defects are reproduced here on purpose, because M0 freezes behaviour
rather than fixing it. They are scheduled for M1:

- The reviews sentiment column is named `sentiment`; the loader expects
  `sentiment_label` and silently invents the missing column as NULL, so
  `ecommerce.reviews.sentiment_label` is NULL for all 99,224 rows.
- `translations_cleaned.csv` is not in the loader's `FILE_CANDIDATES` list, so the
  loader cannot resolve it and synthesises `product_category_translations` from the
  distinct product categories instead, with no English names.

---

## 5. Start the data services

```powershell
docker compose up -d postgres neo4j qdrant
docker compose ps
```

Wait until `postgres` and `neo4j` report `healthy`. Qdrant has no healthcheck; check
it directly:

```powershell
curl http://localhost:6333/collections
```

---

## 6. Load PostgreSQL

Create the schema first. `database/postgres/schema.sql` is the committed schema and
defines primary keys, foreign keys and NOT NULL constraints.

```powershell
Get-Content database/postgres/schema.sql | docker exec -i enterprise_ai_postgres_deploy psql -U enterprise_user -d enterprise_ai
```

Then load, build the views, build the indexes, and validate:

```powershell
python src/database/postgres_loader.py
Get-Content database/postgres/views.sql   | docker exec -i enterprise_ai_postgres_deploy psql -U enterprise_user -d enterprise_ai
Get-Content database/postgres/indexes.sql | docker exec -i enterprise_ai_postgres_deploy psql -U enterprise_user -d enterprise_ai
python src/database/postgres_validator.py
python src/database/sql_view_validator.py
```

Expected row counts:

| Table | Rows |
|---|---|
| customers | 99,441 |
| sellers | 3,095 |
| products | 32,340 |
| orders | 99,441 |
| order_items | 111,046 |
| payments | 103,886 |
| reviews | 99,224 |
| product_category_translations | 73 |

> **Divergence worth knowing about, now verified.** `README.md` §20 documents only
> `CREATE SCHEMA IF NOT EXISTS ecommerce;` and never runs `schema.sql`. The loader
> uses `pandas.to_sql(if_exists="append")`, which creates any missing table *without*
> primary keys, foreign keys or NOT NULL constraints. The original development
> database was built that way and has **zero table constraints** — confirmed by
> querying `information_schema.table_constraints`, which returns no rows for the
> `ecommerce` schema.
>
> The clean-clone run applied `schema.sql` first and then loaded. It worked: 8 primary
> keys, 7 foreign keys and 16 check constraints were created, and all eight tables
> loaded to the exact same row counts with no constraint violations. So the constrained
> path is safe and is what this document recommends. Row counts and column values are
> identical either way; only the constraints differ.

## 7. Load Neo4j (Olist graph)

```powershell
Get-Content database/neo4j/constraints.cypher | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
Get-Content database/neo4j/indexes.cypher     | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
python src/graph/neo4j_loader.py
python src/graph/neo4j_validator.py
```

`neo4j_loader.py` reads from PostgreSQL, so step 6 must be complete.

---

## 8. Generate the synthetic corpus

**This step is required and was not documented before M0.** Both the synthetic graph
loader and the Qdrant ingest read its output; without it, step 9 fails.

```powershell
python src/synthetic/synthetic_data_generator.py
```

Writes six JSONL files into `data/synthetic/` (gitignored):

```
support_tickets.jsonl          3000
customer_emails.jsonl          3000
warranty_claims.jsonl          1000
logistics_incidents.jsonl      1000
policy_documents.jsonl           79
troubleshooting_guides.jsonl     73
```

> **The generator seeds `random` but is not deterministic.** Measured on the
> clean-clone run by diffing its corpus against the original, record by record:
>
> | Artifact | Records | Differ | Differ ignoring `created_at` |
> |---|---|---|---|
> | support_tickets | 3,000 | 10 | 10 |
> | logistics_incidents | 1,000 | 259 | **259** |
> | customer_emails | 3,000 | 4 | 4 |
> | warranty_claims | 1,000 | 1 | 1 |
> | policy_documents | 79 | 79 | 0 |
> | troubleshooting_guides | 73 | 73 | 0 |
>
> The ID sets are stable — no artifact ID appears in one run and not the other. What
> moves is which real order each ID points at: one selection query orders by a
> non-unique column, so 25.9% of logistics incidents attach to a different order on
> regeneration. Policy and guide differences are `created_at` only, from
> `datetime.now()`, and are cosmetic.
>
> The practical consequence: regenerating produces a corpus that disagrees with
> previously saved reports, and with the Neo4j nodes and Qdrant points keyed on those
> IDs. Scheduled for M1.

## 9. Load the synthetic graph and the vector store

```powershell
Get-Content database/neo4j/synthetic_constraints.cypher | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
Get-Content database/neo4j/synthetic_indexes.cypher     | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
python src/graph/synthetic_neo4j_loader.py
python src/graph/synthetic_graph_validator.py

python src/vector/qdrant_ingest.py
python src/vector/qdrant_validator.py
```

`qdrant_ingest.py` downloads `sentence-transformers/all-MiniLM-L6-v2` (~90 MB) on
first run and embeds 8,152 documents. Allow several minutes on CPU.

The pinned model revision is recorded in `.env.example` as
`EMBEDDING_MODEL_REVISION=1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, and it is read
at runtime: every `SentenceTransformer()` construction — ingest, query and the
Qdrant validator — passes `revision=`, so a moved `refs/main` upstream cannot
silently change the vectors on either side of the store. `pytest` checks the local
cache against the pin and fails if any load site drops the `revision=` argument.

---

## 10. Start the API and the UI

```powershell
docker compose build api ui
docker compose up -d
docker compose ps
```

- API: <http://localhost:8000> (`GET /health`, `POST /query`)
- UI: <http://localhost:8501>

> `GET /health` returns a hardcoded `{"status":"ok"}` and checks nothing. During the
> audit it returned `ok` while all four dependencies were unreachable and every
> `/query` was returning 500. Do not use it to decide whether the stack is up; use
> `docker compose ps` and a real `POST /query`. Scheduled for M6.

---

## 11. Verify the stack end to end

```powershell
python src/retrieval/hybrid_retrieval_validator.py
python src/retrieval/retrieval_context_validator.py
python src/generation/answer_generation_validator.py
python src/orchestration/agentic_workflow_validator.py
python src/api/api_validator.py
```

Then run the tests that need the live stack:

```powershell
pytest
```

`tests/test_frozen_defects.py` asserts the current row counts and the three
NULL-column defects. If those tests fail on a fresh install, the data does not match
the frozen baseline.

> What "PASS" from those validators does and does not mean: the five workflow cases
> are byte-identical to the five worked examples embedded in the planner's own
> prompt, the same five cases appear in three different validators, and the
> deterministic half of the pass criterion reduces to "is the answer at least 1,000
> characters long". Treat a green run as "the pipeline executes", not as
> "the answers are correct". Real evaluation is M3.

---

## 12. Regenerate the frozen baseline (optional)

```powershell
python scripts/capture_baseline.py
```

Runs ten questions through the full workflow and writes
`tests/baseline/baseline_results.json`. **Do not commit a regenerated baseline over
the M0 one.** That file is the before-state every later milestone is measured
against; write new captures to a different path with `--output`.

---

## Teardown

```powershell
docker compose down            # keep the data
docker compose down -v         # delete the volumes as well
```

`docker compose down -v` destroys the Postgres, Neo4j and Qdrant volumes. Recovering
means repeating steps 4 to 9.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `FileNotFoundError` for `*_cleaned.csv` | Step 4 not run |
| `synthetic_neo4j_loader.py` finds no input | Step 8 not run — the step README §20 omits |
| `/query` returns 500, `/health` still says `ok` | `/health` checks nothing. Read the API logs: `docker compose logs api` |
| API container cannot resolve `postgres` | Compose network DNS. `docker compose down && docker compose up -d` |
| `psycopg2.OperationalError` from a host script | Postgres not healthy yet, or 5432 taken by a local install |
| Model downloads on every run | The container has no HuggingFace cache mount; the model is re-fetched per container build |
| `Bind for 127.0.0.1:8000 failed: port is already allocated` | Something else owns 8000. `docker ps --filter publish=8000`. Compose leaves the other services up, so `docker compose ps` looks healthy while the API is absent |
| `pytest` fails on row counts in `test_frozen_defects.py` | The database does not match the M0 baseline. Re-run steps 4 to 9 |
