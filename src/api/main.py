from __future__ import annotations

import os
import re
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
API_DIR = PROJECT_ROOT / "src" / "api"
ORCHESTRATION_DIR = PROJECT_ROOT / "src" / "orchestration"

RETRIEVAL_DIR = PROJECT_ROOT / "src" / "retrieval"
OBSERVABILITY_DIR = PROJECT_ROOT / "src" / "observability"

# API_DIR is here so `import demo` resolves under `uvicorn src.api.main:app`, where
# only /app is on sys.path. The tests import this module as bare `main` with the same
# directory already on the path, so one import form works in both.
for _directory in (API_DIR, ORCHESTRATION_DIR, RETRIEVAL_DIR, OBSERVABILITY_DIR):
    if str(_directory) not in sys.path:
        sys.path.append(str(_directory))


import demo  # stdlib only, and it must be importable with no model stack present

from otel import memory_store, span_to_dict
from query_cache import get_cache

# M8 Task 3. "Zero LLM calls and zero database queries" is enforced here rather than
# asserted: in demo mode the modules that could make one are never imported. torch,
# SQLAlchemy, psycopg2, the Neo4j and Qdrant drivers and google-genai all arrive
# through these two lines and nowhere else, and requirements-demo.txt does not install
# them at all -- so a demo container physically cannot reach a model or a database.
DEMO_MODE = demo.demo_mode_enabled()

if DEMO_MODE:
    run_agentic_workflow = None
    get_embedding_model = None
    load_settings = None
else:
    from agentic_workflow import run_agentic_workflow
    from hybrid_retriever import get_embedding_model, load_settings



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
    # M8 Task 3: demo mode is deliberately unauthenticated. The point of the mode is
    # that someone with the link can use it, and there is nothing behind the gate to
    # protect -- no model call to bill, no database to reach, and the payload is
    # recorded output that ships in the repository. Live mode is unchanged.
    if DEMO_MODE:
        return "demo"

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
    # The handle for GET /traces/{run_id}. Without it a caller cannot ask for the
    # trace of the request it just made.
    run_id: str
    query: str

    # A refusal is an outcome, not an error. `refusal_node` returns a response with
    # no answer_provider, answer_model, answer_length_chars, evaluation_summary or
    # output_files -- it never generated an answer, so those fields do not exist.
    # This model listed all five as required, so building it raised
    # KeyError: 'answer_provider' and the caller got a 500 with an opaque incident
    # id. Every correctly refused question looked like a server crash.
    answerable: bool
    refusal_reason: str | None

    answer_provider: str
    answer_model: str
    answer_length_chars: int
    source_summary: dict[str, Any]
    evaluation_summary: dict[str, Any]
    output_files: dict[str, str]
    answer_preview: str

    # Cost and latency, aggregated from this run's spans. The UI shows them per query
    # (M8 Task 4) and they are the same numbers GET /traces/{run_id} is built from.
    # Empty rather than zeroed when the run is no longer in the span store: unknown
    # cost and no cost are different facts.
    metrics: dict[str, Any]

    @classmethod
    def from_workflow(cls, result: dict[str, Any]) -> "QueryResponse":
        """One constructor for both outcomes, so they cannot drift apart.

        The demo replay (M8 Task 3) goes through this too -- that is what makes a
        replayed response structurally identical to a live one rather than
        similar-looking.
        """
        answerable = result.get("answerable", True)

        return cls(
            overall_status=result["overall_status"],
            run_id=result["run_id"],
            query=result["query"],
            answerable=answerable,
            refusal_reason=result.get("refusal_reason"),
            # "none" rather than "gemini": no model was called, and naming one here
            # would put a provider on a response no provider produced.
            answer_provider=result.get("answer_provider") or "none",
            answer_model=result.get("answer_model") or "none",
            answer_length_chars=int(
                result.get("answer_length_chars")
                or len(result.get("answer_preview") or "")
            ),
            source_summary=result.get("source_summary") or {},
            evaluation_summary=result.get("evaluation_summary") or {},
            output_files=result.get("output_files") or {},
            answer_preview=result.get("answer_preview") or "",
            metrics=result.get("metrics") or {},
        )


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
    if DEMO_MODE:
        # Nothing to warm: demo mode never encodes anything. Fail here rather than at
        # the first request if the bundle is missing -- a demo deployment with no
        # recordings should not come up green.
        info = demo.bundle_info()
        application.state.embedding_model = None
        application.state.demo_bundle = info

        print(
            f"DEMO_MODE: replaying {info['scenario_count']} recorded runs from "
            f"{info['source_run']}. No model and no database will be called."
        )

        yield
        return

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
        "mode": "demo" if DEMO_MODE else "live",
        "version": "0.1.0",
        "endpoints": {
            "health": "GET /health",
            "query": "POST /query",
            "traces": "GET /traces/{run_id}",
            "scenarios": "GET /demo/scenarios",
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
    if DEMO_MODE:
        # Running the probes here would import drivers the demo image does not
        # install, report three dependencies "down", and return 503 -- for a
        # deployment that is working exactly as intended.
        return {
            "status": "ok",
            "service": "enterprise-agentic-workflow-api",
            "mode": "demo",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dependencies": {},
            "failed": [],
            "not_checked": {
                "postgres": "demo mode: no database is connected",
                "neo4j": "demo mode: no database is connected",
                "qdrant": "demo mode: no database is connected",
                "gemini": "demo mode: no model is called",
            },
            "demo": demo.bundle_info(),
        }

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
    if DEMO_MODE:
        # No try/except around this: it touches no network and no disk beyond one
        # JSON file read at startup, so there is no failure here worth sanitising.
        return QueryResponse.from_workflow(demo.replay(request.query))

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

        return QueryResponse.from_workflow(result)

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


# --------------------------------------------------------------------------------
# Trace viewer (M7 Task 4)
# --------------------------------------------------------------------------------
#
# The M7 exit criterion is that a failing run is diagnosable from the trace alone,
# without re-running it. That requires the trace to be *reachable*, so this reads the
# in-memory span store the OTel provider exports to alongside console or OTLP.
#
# The store is bounded at 50 runs and lives in the API process. It is a viewer, not a
# backend: for retention, set OTEL_EXPORTER=otlp and read the collector. A run that has
# aged out returns 404, which is honest -- an empty span list would read as "this run
# produced no spans", which is a different and much worse claim.

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class TraceResponse(BaseModel):
    run_id: str
    span_count: int
    root_span_id: str | None
    trace_id: str | None
    status: str | None
    duration_ms: float | None
    spans: list[dict[str, Any]]


@app.get("/traces/{run_id}", response_model=TraceResponse)
def get_trace(
    run_id: str,
    _token: str = Depends(require_bearer_token),
) -> TraceResponse:
    """The OTel spans for one run: name, status, attributes, duration and parent."""
    if not RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=400, detail="Malformed run_id.")

    if DEMO_MODE:
        recorded = demo.trace_for(run_id)

        if recorded is None:
            raise HTTPException(
                status_code=404,
                detail=f"Demo mode holds no recorded trace for run_id {run_id!r}.",
            )

        root = next((s for s in recorded if s["parent_span_id"] is None), None)

        return TraceResponse(
            run_id=run_id,
            span_count=len(recorded),
            root_span_id=root["span_id"] if root else None,
            trace_id=recorded[0]["trace_id"] if recorded else None,
            status=root["status"] if root else None,
            duration_ms=root["duration_ms"] if root else None,
            spans=recorded,
        )

    spans = memory_store().spans_for(run_id)

    if not spans:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No trace held for run_id {run_id!r}. The in-memory store keeps the "
                f"{memory_store().max_runs} most recent runs of this process."
            ),
        )

    # Spans arrive in completion order, which puts a parent after every one of its
    # children. Chronological order is the fix -- but start_time alone is not enough:
    # `time_ns()` resolves to ~0.4 ms on this Windows host, and a parent and the child
    # opened immediately inside it land on the same tick. Measured on a live run:
    # `retrieval` and `retrieval.vector` shared a start_time to the nanosecond, and a
    # stable sort then listed the child above its own parent. Depth breaks the tie, so
    # a parent always precedes its children no matter how fast the clock is.
    payload = [span_to_dict(span) for span in spans]
    starts = {d["span_id"]: (s.start_time or 0) for d, s in zip(payload, spans)}
    parents = {s["span_id"]: s["parent_span_id"] for s in payload}

    def depth(span_id: str) -> int:
        seen, levels = set(), 0
        parent = parents.get(span_id)

        while parent is not None and parent in parents and parent not in seen:
            seen.add(parent)
            levels += 1
            parent = parents.get(parent)

        return levels

    payload.sort(key=lambda s: (starts[s["span_id"]], depth(s["span_id"])))

    root = next((s for s in payload if s["parent_span_id"] is None), None)

    return TraceResponse(
        run_id=run_id,
        span_count=len(payload),
        root_span_id=root["span_id"] if root else None,
        trace_id=payload[0]["trace_id"] if payload else None,
        status=root["status"] if root else None,
        duration_ms=root["duration_ms"] if root else None,
        spans=payload,
    )


# --------------------------------------------------------------------------------
# Demo mode (M8 Task 3)
# --------------------------------------------------------------------------------


class DemoScenariosResponse(BaseModel):
    mode: str
    bundle: dict[str, Any]
    scenarios: list[dict[str, Any]]


@app.get("/demo/scenarios", response_model=DemoScenariosResponse)
def demo_scenarios() -> DemoScenariosResponse:
    """The questions this deployment can replay, with their routes and outcomes.

    Available in live mode too, where it answers "what would the demo show?" without
    needing a second deployment to find out.
    """
    return DemoScenariosResponse(
        mode="demo" if DEMO_MODE else "live",
        bundle=demo.bundle_info(),
        scenarios=demo.scenario_summaries(),
    )
