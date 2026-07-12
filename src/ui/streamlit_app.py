from __future__ import annotations

from datetime import datetime
from typing import Any
import os
import requests
import streamlit as st


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")


EXAMPLE_QUERIES = [
    "Find sellers with negative customer complaints, warranty issues, and relevant support policies",
    "Investigate late delivery logistics incidents by customer region and find troubleshooting guidance",
    "Find payment questions, refund policies, and customer support cases",
    "Investigate product quality complaints and find troubleshooting procedures",
    "Show seller ticket paths for negative complaints and product issues",
]


def call_health(api_base_url: str) -> dict[str, Any]:
    response = requests.get(
        f"{api_base_url.rstrip('/')}/health",
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def call_query(api_base_url: str, query: str) -> dict[str, Any]:
    response = requests.post(
        f"{api_base_url.rstrip('/')}/query",
        json={"query": query},
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def safe_list(value: Any) -> list[Any]:
    if value is None:
        return []

    if isinstance(value, list):
        return value

    return [value]


def render_status_banner(result: dict[str, Any]) -> None:
    workflow_status = result.get("overall_status", "N/A")

    if workflow_status == "PASS":
        st.success("Agentic workflow completed successfully.")
    else:
        st.error(f"Agentic workflow completed with status: {workflow_status}")


def render_workflow_metrics(result: dict[str, Any]) -> None:
    st.markdown("## Workflow Summary")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Workflow Status", result.get("overall_status", "N/A"))

    with col2:
        st.metric("Answer Provider", result.get("answer_provider", "N/A"))

    with col3:
        st.metric("Answer Model", result.get("answer_model", "N/A"))

    with col4:
        st.metric("Answer Chars", result.get("answer_length_chars", 0))


def render_evaluation_summary(evaluation_summary: dict[str, Any]) -> None:
    st.markdown("## Answer Evaluation")

    if not evaluation_summary:
        st.warning("No evaluation summary returned by the API.")
        return

    eval_col1, eval_col2, eval_col3, eval_col4 = st.columns(4)

    with eval_col1:
        st.metric("Eval Status", evaluation_summary.get("overall_status", "N/A"))

    with eval_col2:
        st.metric("Grounding", evaluation_summary.get("grounding_score", "N/A"))

    with eval_col3:
        st.metric("Completeness", evaluation_summary.get("completeness_score", "N/A"))

    with eval_col4:
        st.metric(
            "Business Ready",
            evaluation_summary.get("business_readiness_score", "N/A"),
        )

    unsupported_claims = safe_list(evaluation_summary.get("unsupported_claims"))
    deterministic_failures = safe_list(evaluation_summary.get("deterministic_failures"))
    missing_evidence = safe_list(evaluation_summary.get("missing_evidence"))

    st.caption(
        f"Unsupported claims: {len(unsupported_claims)} | "
        f"Missing evidence items: {len(missing_evidence)} | "
        f"Deterministic validation warnings: {len(deterministic_failures)}"
    )

    if unsupported_claims:
        st.warning("Unsupported claims were detected.")
        with st.expander("Unsupported claims"):
            st.json(unsupported_claims)

    if deterministic_failures:
        st.info("Deterministic validation warnings were detected.")
        with st.expander("Deterministic validation warnings"):
            st.json(deterministic_failures)

    with st.expander("Full evaluation details"):
        st.json(evaluation_summary)


def render_source_summary(source_summary: dict[str, Any]) -> None:
    st.markdown("## Routing Summary")

    sql_summary = source_summary.get("sql", {})
    graph_summary = source_summary.get("graph", {})
    vector_summary = source_summary.get("vector", {})

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric(
            label="SQL Route",
            value=sql_summary.get("intent", "N/A"),
        )
        st.caption("PostgreSQL structured business facts")

    with col2:
        st.metric(
            label="Graph Route",
            value=graph_summary.get("intent", "N/A"),
        )
        st.caption("Neo4j connected relationship reasoning")

    with col3:
        vector_groups = vector_summary.get("artifact_groups", [])
        st.metric(
            label="Vector Groups",
            value=len(vector_groups),
        )
        st.caption(", ".join(vector_groups) if vector_groups else "N/A")

    with st.expander("Full source summary"):
        st.json(source_summary)


def render_output_files(output_files: dict[str, str]) -> None:
    if not output_files:
        st.info("No output files returned.")
        return

    for label, path in output_files.items():
        st.code(f"{label}: {path}", language="text")


def render_answer_preview(result: dict[str, Any]) -> None:
    st.markdown("## Grounded Answer Preview")

    answer_preview = result.get("answer_preview")

    if not answer_preview:
        st.warning("No answer preview returned.")
        return

    st.markdown(answer_preview)


def render_response(result: dict[str, Any]) -> None:
    render_status_banner(result)
    render_workflow_metrics(result)
    render_evaluation_summary(result.get("evaluation_summary", {}))
    render_source_summary(result.get("source_summary", {}))
    render_answer_preview(result)

    with st.expander("Output files"):
        render_output_files(result.get("output_files", {}))

    with st.expander("Raw API response"):
        st.json(result)


def render_sidebar() -> tuple[str, str | None]:
    with st.sidebar:
        st.header("Backend Settings")

        api_base_url = st.text_input(
            "FastAPI base URL",
            value=API_BASE_URL,
            help="Make sure the FastAPI server is running before sending a query.",
        )

        if st.button("Check API Health"):
            try:
                health = call_health(api_base_url)
                st.success("API is healthy.")
                st.json(health)
            except Exception as exc:
                st.error(f"API health check failed: {exc}")

        st.markdown("---")
        st.header("Example Queries")

        selected_example = st.selectbox(
            "Choose an example",
            EXAMPLE_QUERIES,
        )

        use_example = st.button("Use Selected Example")

        if use_example:
            return api_base_url, selected_example

        return api_base_url, None


def render_architecture() -> None:
    st.markdown("### System Architecture")

    st.code(
        """
Streamlit UI
   ↓
FastAPI Backend
   ↓
LangGraph Agentic Workflow
   ↓
Gemini Planner Agent
   ↓
Hybrid Retrieval Tools
   ├── PostgreSQL: structured business facts
   ├── Neo4j: graph-connected reasoning
   └── Qdrant: semantic document retrieval
   ↓
Retrieval Context Builder
   ↓
Gemini Answer Agent
   ↓
Gemini Evaluation Agent
   ↓
Final Grounded Business Answer
        """.strip(),
        language="text",
    )


def main() -> None:
    st.set_page_config(
        page_title="Enterprise AI Intelligence Platform",
        page_icon="🧠",
        layout="wide",
    )

    st.title("Enterprise Multi-Agent Knowledge Intelligence Platform")
    st.caption(
        "LLM-planned enterprise workflow using Gemini, LangGraph, PostgreSQL, Neo4j, Qdrant, and FastAPI."
    )

    api_base_url, selected_example = render_sidebar()

    if "query_text" not in st.session_state:
        st.session_state.query_text = EXAMPLE_QUERIES[0]

    if selected_example:
        st.session_state.query_text = selected_example

    st.markdown("## Ask a Business Question")

    query = st.text_area(
        "Question",
        value=st.session_state.query_text,
        height=120,
        key="query_text_area",
        help="Enter a natural-language enterprise business question.",
    )

    run_query = st.button(
        "Run Agentic Workflow",
        type="primary",
    )

    if run_query:
        if not query.strip():
            st.warning("Please enter a business question.")
            return

        st.session_state.query_text = query

        with st.spinner("Running Gemini-planned agentic workflow through FastAPI..."):
            try:
                started_at = datetime.now()
                result = call_query(api_base_url, query.strip())
                finished_at = datetime.now()
                duration_seconds = (finished_at - started_at).total_seconds()

                st.caption(f"Completed in {duration_seconds:.2f} seconds.")
                render_response(result)

            except requests.exceptions.ConnectionError:
                st.error(
                    "Could not connect to FastAPI. "
                    "Make sure the API server is running with: "
                    "`uvicorn src.api.main:app --reload --port 8000`"
                )

            except requests.exceptions.HTTPError as exc:
                st.error(f"API returned an HTTP error: {exc}")
                try:
                    st.json(exc.response.json())
                except Exception:
                    st.write(exc.response.text)

            except Exception as exc:
                st.error(f"Unexpected error: {exc}")

    st.markdown("---")
    render_architecture()


if __name__ == "__main__":
    main()