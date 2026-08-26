from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATION_DIR = PROJECT_ROOT / "src" / "orchestration"

RETRIEVAL_DIR = PROJECT_ROOT / "src" / "retrieval"
OBSERVABILITY_DIR = PROJECT_ROOT / "src" / "observability"

for _directory in (ORCHESTRATION_DIR, RETRIEVAL_DIR, OBSERVABILITY_DIR):
    if str(_directory) not in sys.path:
        sys.path.append(str(_directory))


from agentic_workflow import run_agentic_workflow
from hybrid_retriever import get_embedding_model, load_settings
from query_cache import get_cache


class QueryRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=5,
        description="Natural-language enterprise business question.",
        examples=[
            "Find sellers with negative customer complaints, warranty issues, and relevant support policies"
        ],
    )


class QueryResponse(BaseModel):
    overall_status: str
    query: str
    answer_provider: str
    answer_model: str
    answer_length_chars: int
    source_summary: dict[str, Any]
    evaluation_summary: dict[str, Any]
    output_files: dict[str, str]
    answer_preview: str


# Measured on the M3 run (docs/M5_COST_TABLE.md): mean tokens for an answered query.
# Used to turn a hit count into an estimated saving.
MEAN_TOKENS_PER_QUERY = {"input": 9515.0, "output": 1067.0}


class CacheStatsResponse(BaseModel):
    backend: str | None
    enabled: bool
    hits: int
    misses: int
    lookups: int
    hit_rate: float
    stores: int
    evictions: int
    entries: int
    max_size: int | None
    estimated_tokens_saved: dict[str, int]
    estimated_cost_saved_usd: float


class HealthResponse(BaseModel):
    status: str
    service: str
    generated_at: str


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Warm the embedding model before the first request (AUDIT.md F-09).

    It was previously constructed inside the per-request retrieval call, costing
    ~1.25s of disk reads on every query against a ~10ms encode. It is immutable and
    the revision is pinned, so one instance per process is correct.
    """
    settings = load_settings()

    started = time.perf_counter()
    application.state.embedding_model = get_embedding_model(settings)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    application.state.embedding_model_name = settings["embedding_model_name"]
    application.state.embedding_model_revision = settings["embedding_model_revision"]
    application.state.startup_model_load_ms = round(elapsed_ms, 1)

    print(
        f"Embedding model warmed at startup in {elapsed_ms:.0f} ms: "
        f"{settings['embedding_model_name']} @ {settings['embedding_model_revision']}"
    )

    yield

    application.state.embedding_model = None


app = FastAPI(
    title="Enterprise Multi-Agent Knowledge Intelligence API",
    description=(
        "FastAPI backend for the enterprise hybrid retrieval and "
        "agentic workflow platform."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "Enterprise Multi-Agent Knowledge Intelligence API",
        "status": "running",
        "version": "0.1.0",
        "endpoints": {
            "health": "GET /health",
            "query": "POST /query",
            "docs": "GET /docs",
        },
    }


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="enterprise-agentic-workflow-api",
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest) -> QueryResponse:
    try:
        safe_query_name = (
            request.query.lower()
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
            .replace("?", "")
            .replace(",", "")
        )

        safe_query_name = safe_query_name[:80]

        output_dir = Path("reports/api_runs") / safe_query_name

        result = run_agentic_workflow(
            query=request.query,
            output_dir=output_dir,
        )

        return QueryResponse(
            overall_status=result["overall_status"],
            query=result["query"],
            answer_provider=result["answer_provider"],
            answer_model=result["answer_model"],
            answer_length_chars=result["answer_length_chars"],
            source_summary=result["source_summary"],
            evaluation_summary=result.get("evaluation_summary", {}),
            output_files=result["output_files"],
            answer_preview=result["answer_preview"],
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to run enterprise workflow: {exc}",
        ) from exc


@app.get("/cache/stats", response_model=CacheStatsResponse)
def cache_stats() -> CacheStatsResponse:
    """Hit/miss counts and the tokens those hits avoided spending.

    The saving is an ESTIMATE: hits x the mean tokens an answered query cost on the
    M3 run. A hit on a refusal actually saves less than that mean, so this is an
    upper bound rather than a measurement of the specific queries served.
    """
    return CacheStatsResponse(**get_cache().as_dict(MEAN_TOKENS_PER_QUERY))
