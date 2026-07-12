from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATION_DIR = PROJECT_ROOT / "src" / "orchestration"

if str(ORCHESTRATION_DIR) not in sys.path:
    sys.path.append(str(ORCHESTRATION_DIR))


from agentic_workflow import run_agentic_workflow


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


class HealthResponse(BaseModel):
    status: str
    service: str
    generated_at: str


app = FastAPI(
    title="Enterprise Multi-Agent Knowledge Intelligence API",
    description=(
        "FastAPI backend for the enterprise hybrid retrieval and "
        "agentic workflow platform."
    ),
    version="0.1.0",
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