from __future__ import annotations

import os
import secrets
import sys
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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



# --------------------------------------------------------------------------------
# Authentication (M6 Task 4)
# --------------------------------------------------------------------------------
#
# AUDIT.md P2/F-08: /query took no credential at all, and its exception handler
# returned `detail=f"Failed to run enterprise workflow: {exc}"` to the caller. For a
# database failure that string carries the host and port -- the audit observed
# `connection to server at "localhost" (::1), port 5432 failed` reaching an
# unauthenticated caller.

_bearer = HTTPBearer(auto_error=False)


def _configured_token() -> str | None:
    token = os.getenv("API_BEARER_TOKEN", "").strip()
    return token or None


def require_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """401 on a missing or wrong token. Never 403, never 500.

    `HTTPBearer(auto_error=False)` because the default raises 403 for a missing
    header, and "you did not authenticate" is 401.
    """
    expected = _configured_token()

    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured on this server.",
        )

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time compare so a wrong token cannot be found byte by byte.
    if not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return credentials.credentials


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



# --------------------------------------------------------------------------------
# Health (M6 Task 5)
# --------------------------------------------------------------------------------
#
# AUDIT.md F-07: /health returned a hardcoded status="ok". During the audit the
# container's DNS failed entirely, every dependency was unreachable and /query was
# returning 500 on 100% of requests, and /health still reported ok.

DEPENDENCY_CHECK_TIMEOUT_SECONDS = 3.0


def _check_postgres() -> None:
    from sqlalchemy import text as sql_text

    from hybrid_retriever import build_postgres_engine

    engine = build_postgres_engine(load_settings())

    try:
        with engine.connect() as connection:
            connection.execute(sql_text("SELECT 1"))
    finally:
        engine.dispose()


def _check_neo4j() -> None:
    from hybrid_retriever import build_neo4j_driver

    driver = build_neo4j_driver(load_settings())

    try:
        with driver.session(database="neo4j", default_access_mode="READ") as session:
            session.execute_read(lambda tx: tx.run("RETURN 1 AS ok").single())
    finally:
        driver.close()


def _check_qdrant() -> None:
    from hybrid_retriever import build_qdrant_client

    settings = load_settings()
    build_qdrant_client(settings).get_collection(settings["qdrant_collection"])


DEPENDENCY_CHECKS = {
    "postgres": _check_postgres,
    "neo4j": _check_neo4j,
    "qdrant": _check_qdrant,
}


def check_dependencies() -> dict[str, dict[str, Any]]:
    """Probe each dependency with the cheapest query that proves it is answering.

    Gemini is deliberately not probed: the cheapest check is a billable generation
    call, and a health endpoint that costs money per poll will be polled less often
    or turned off. Its absence is stated in the response rather than implied.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    results: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=len(DEPENDENCY_CHECKS)) as pool:
        futures = {
            name: pool.submit(check) for name, check in DEPENDENCY_CHECKS.items()
        }

        for name, future in futures.items():
            started = time.perf_counter()

            try:
                future.result(timeout=DEPENDENCY_CHECK_TIMEOUT_SECONDS)
                results[name] = {
                    "status": "ok",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            except FuturesTimeout:
                results[name] = {
                    "status": "down",
                    "error": f"did not respond within {DEPENDENCY_CHECK_TIMEOUT_SECONDS}s",
                }
            except Exception as exc:  # noqa: BLE001
                # The dependency name and failure class are operational facts a
                # health endpoint exists to report. The message is truncated so a
                # driver error cannot spill a full connection string.
                results[name] = {
                    "status": "down",
                    "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}",
                }

    return results


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    """200 only if every checked dependency answers; 503 with the failures named."""
    dependencies = check_dependencies()
    failed = sorted(n for n, r in dependencies.items() if r["status"] != "ok")

    if failed:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "degraded" if failed else "ok",
        "service": "enterprise-agentic-workflow-api",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dependencies": dependencies,
        "failed": failed,
        "not_checked": {"gemini": "not probed: the cheapest check is a billable call"},
    }


@app.post("/query", response_model=QueryResponse)
def query(
    request: QueryRequest,
    _token: str = Depends(require_bearer_token),
) -> QueryResponse:
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
        # The caller gets an opaque reference; the detail goes to the server log.
        # The audit observed a psycopg2 OperationalError -- host and port included --
        # returned verbatim to an unauthenticated caller (F-08).
        incident = uuid.uuid4().hex[:12]

        print(
            f"[incident {incident}] /query failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "The request could not be completed.",
                "incident_id": incident,
            },
        ) from exc


@app.get("/cache/stats", response_model=CacheStatsResponse)
def cache_stats() -> CacheStatsResponse:
    """Hit/miss counts and the tokens those hits avoided spending.

    The saving is an ESTIMATE: hits x the mean tokens an answered query cost on the
    M3 run. A hit on a refusal actually saves less than that mean, so this is an
    upper bound rather than a measurement of the specific queries served.
    """
    return CacheStatsResponse(**get_cache().as_dict(MEAN_TOKENS_PER_QUERY))
