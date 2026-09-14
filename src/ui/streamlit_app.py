"""Streamlit front end (M8 Task 4).

The rebuild plan's line for this milestone is "the evidence trail *is* the product".
So the page leads with the trail -- which route the planner chose, how many records each
store actually returned, what the run cost, how long it took, and a link to the spans --
and puts the answer text below it. An answer with no visible evidence is the thing this
project exists to stop shipping.

Everything shown here comes from one POST /query response plus one GET /traces/{run_id}.
Nothing is recomputed in the UI, so the page cannot disagree with the API.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests
import streamlit as st

def normalise_base_url(value: str, default: str = "") -> str:
    """Accept a bare hostname as well as a full URL.

    Render's blueprint injects a service's address as a hostname with no scheme
    (`enterprise-ai-demo-api.onrender.com`). `requests` rejects that, and the failure
    at deploy time reads as "the API is down" rather than "the URL has no scheme".
    A bare host is assumed to be https, which is what any hosted service will be;
    localhost and 127.0.0.1 are not.
    """
    value = (value or default).strip().rstrip("/")

    if not value:
        return ""

    if "://" in value:
        return value

    local = value.split(":")[0] in {"localhost", "127.0.0.1", "0.0.0.0"}

    return f"{'http' if local else 'https'}://{value}"


API_BASE_URL = normalise_base_url(os.getenv("API_BASE_URL", ""), "http://127.0.0.1:8000")

# M6 Task 4 put a bearer token in front of POST /query. The UI was never updated, so
# every query from this page returned 401 against an authenticated API -- found while
# preparing the M8 deployment, where the UI is the only way in. Demo deployments run
# unauthenticated and leave this empty.
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN", "")

# The public URL of the API, when it differs from the one this container calls
# internally. The trace links have to be clickable from the visitor's browser, not
# from inside the compose network.
PUBLIC_API_BASE_URL = normalise_base_url(os.getenv("PUBLIC_API_BASE_URL", ""))

FALLBACK_EXAMPLES = [
    "Find sellers with negative customer complaints, warranty issues, and relevant support policies",
    "Investigate late delivery logistics incidents by customer region and find troubleshooting guidance",
    "Which sellers have the worst on-time delivery record?",
]

LEG_LABELS = {
    "sql": ("PostgreSQL", "structured business facts"),
    "graph": ("Neo4j", "connected entity paths"),
    "vector": ("Qdrant", "semantic document retrieval"),
}


def auth_headers() -> dict[str, str]:
    """No header at all when no token is configured.

    Sending `Bearer ` with an empty token would turn a clear 401 "missing bearer
    token" into a confusing 401 "invalid bearer token".
    """
    if not API_BEARER_TOKEN:
        return {}

    return {"Authorization": f"Bearer {API_BEARER_TOKEN}"}


# --------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------


def call_health(api_base_url: str) -> dict[str, Any]:
    response = requests.get(f"{api_base_url.rstrip('/')}/health", timeout=30)
    response.raise_for_status()
    return response.json()


def call_query(api_base_url: str, query: str) -> dict[str, Any]:
    response = requests.post(
        f"{api_base_url.rstrip('/')}/query",
        json={"query": query},
        headers=auth_headers(),
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def call_trace(api_base_url: str, run_id: str) -> dict[str, Any] | None:
    """The trace is supporting detail, so a failure here must not lose the answer."""
    try:
        response = requests.get(
            f"{api_base_url.rstrip('/')}/traces/{run_id}",
            headers=auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


@st.cache_data(ttl=300)
def call_scenarios(api_base_url: str) -> dict[str, Any] | None:
    try:
        response = requests.get(
            f"{api_base_url.rstrip('/')}/demo/scenarios",
            headers=auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def trace_url(api_base_url: str, run_id: str) -> str:
    base = PUBLIC_API_BASE_URL or api_base_url.rstrip("/")
    return f"{base}/traces/{run_id}"


def safe_list(value: Any) -> list[Any]:
    if value is None:
        return []

    return value if isinstance(value, list) else [value]


# --------------------------------------------------------------------------------
# The evidence trail
# --------------------------------------------------------------------------------


def render_status_banner(result: dict[str, Any]) -> None:
    status = result.get("overall_status", "N/A")

    if status == "PASS":
        st.success("Answered, and the evaluator passed it.")
    elif status == "REFUSED":
        st.info(f"Refused. {result.get('refusal_reason') or ''}")
    elif status == "DEMO_NO_SCENARIO":
        st.warning(result.get("refusal_reason") or "No recorded run matches this question.")
    else:
        st.error(f"Answered with status: {status}")


def render_cost_and_latency(result: dict[str, Any], api_base_url: str) -> None:
    metrics = result.get("metrics") or {}
    run_id = result.get("run_id", "")

    col1, col2, col3, col4 = st.columns(4)

    cost = metrics.get("cost_usd")
    latency = metrics.get("latency_ms")

    # An unknown cost and a zero cost are different facts, and a dash says so.
    col1.metric("Cost", f"${cost:.6f}" if cost is not None else "—")
    col2.metric("Latency", f"{latency / 1000:.1f} s" if latency else "—")
    col3.metric(
        "Tokens",
        f"{metrics.get('input_tokens', 0):,} in / {metrics.get('output_tokens', 0):,} out"
        if metrics else "—",
    )
    col4.metric("LLM calls", metrics.get("llm_calls", "—"))

    if metrics.get("usage_missing_calls"):
        st.caption(
            f"{metrics['usage_missing_calls']} call(s) returned no usage figures, so "
            f"the cost above is a lower bound rather than a measurement."
        )

    if run_id:
        st.caption(f"`run_id` **{run_id}** — full trace: {trace_url(api_base_url, run_id)}")


def render_route(result: dict[str, Any]) -> None:
    """Which tools the planner chose, and what each one actually returned."""
    st.markdown("### Route the planner chose")

    summary = result.get("source_summary") or {}

    if not summary:
        st.caption("No route was selected — no store was queried.")
        return

    columns = st.columns(3)

    for column, leg in zip(columns, ("sql", "graph", "vector")):
        detail = summary.get(leg) or {}
        store, role = LEG_LABELS[leg]

        if leg == "vector":
            groups = detail.get("artifact_groups") or []
            chosen = ", ".join(groups) if groups else "not used"
        else:
            chosen = detail.get("intent") or "not used"

        records = detail.get("records", 0)
        skipped = detail.get("skipped") or chosen == "not used"

        with column:
            st.markdown(f"**{store}**")
            st.markdown(f"`{chosen}`")
            st.metric("records returned", records if not skipped else 0)
            st.caption(role)

            if detail.get("sort_by"):
                st.caption(f"sorted by `{detail['sort_by']}`")

            filters = detail.get("filters") or {}

            if filters:
                for name, values in filters.items():
                    count = len(values) if isinstance(values, list) else 1
                    st.caption(f"filtered on `{name}` ({count})")

            if detail.get("id_lookups"):
                st.caption(
                    f"ID lookup: {len(detail['id_lookups'])} requested, "
                    f"{detail.get('id_lookup_hits', 0)} found"
                )

    total = sum(
        (summary.get(leg) or {}).get("records", 0) for leg in ("sql", "graph", "vector")
    )

    if total == 0:
        st.warning(
            "No store returned a record. Any answer below would rest on nothing — "
            "which is why the refusal gate exists."
        )


def render_trace(result: dict[str, Any], api_base_url: str) -> None:
    run_id = result.get("run_id")

    if not run_id or run_id == "demo_no_scenario":
        return

    trace = call_trace(api_base_url, run_id)

    if trace is None:
        st.caption("The trace for this run is no longer held by the API.")
        return

    st.markdown("### Where the time went")

    spans = trace.get("spans") or []
    longest = max((s.get("duration_ms") or 0) for s in spans) or 1

    for span in spans:
        duration = span.get("duration_ms") or 0
        depth = 0 if span.get("parent_span_id") is None else 1
        indent = "&nbsp;" * (4 * depth)
        status = span.get("status", "")
        mark = "" if status == "OK" else f" **{status}**"

        st.markdown(
            f"{indent}`{span.get('name','')}`{mark} — {duration:,.0f} ms",
            unsafe_allow_html=True,
        )
        st.progress(min(duration / longest, 1.0))

    if trace.get("reconstructed"):
        st.caption("Reconstructed from the recorded event log.")

    with st.expander("Raw spans"):
        st.json(trace)


def render_evaluation(evaluation: dict[str, Any]) -> None:
    st.markdown("### What the evaluator said about the answer")

    if not evaluation:
        st.caption("No answer was generated, so there was nothing to evaluate.")
        return

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Verdict", evaluation.get("overall_status", "—"))
    col2.metric("Grounding", f"{evaluation.get('grounding_score', '—')} / 5")
    col3.metric("Completeness", f"{evaluation.get('completeness_score', '—')} / 5")
    col4.metric("Business ready", f"{evaluation.get('business_readiness_score', '—')} / 5")

    unsupported = safe_list(evaluation.get("unsupported_claims"))
    deterministic = safe_list(evaluation.get("deterministic_failures"))
    missing = safe_list(evaluation.get("missing_evidence"))

    if unsupported:
        st.warning(f"{len(unsupported)} claim(s) the evaluator could not tie to evidence.")
        with st.expander("Unsupported claims"):
            st.json(unsupported)

    if deterministic:
        st.info(f"{len(deterministic)} deterministic validation warning(s).")
        with st.expander("Deterministic validation warnings"):
            st.json(deterministic)

    if missing:
        with st.expander(f"Missing evidence ({len(missing)})"):
            st.json(missing)

    st.caption(
        "The judge is a Gemini call scoring this system's own answer. Its agreement "
        "with a human rater measured kappa 0.390 (docs/M3_FINDINGS.md), below the 0.7 "
        "floor — read these scores as a weak signal, not a verdict."
    )

    with st.expander("Full evaluation"):
        st.json(evaluation)


def render_answer(result: dict[str, Any]) -> None:
    st.markdown("### Answer")

    if not result.get("answerable", True):
        st.markdown(f"> {result.get('refusal_reason') or 'Refused.'}")
        st.caption(
            f"No answer was generated. Provider: `{result.get('answer_provider')}`."
        )
        return

    answer = result.get("answer_preview")

    if not answer:
        st.warning("No answer text was returned.")
        return

    st.markdown(answer)
    st.caption(
        f"{result.get('answer_length_chars', 0):,} characters from "
        f"`{result.get('answer_model')}` via `{result.get('answer_provider')}`."
    )


def render_response(result: dict[str, Any], api_base_url: str) -> None:
    render_status_banner(result)
    render_cost_and_latency(result, api_base_url)

    st.markdown("---")
    st.markdown("## Evidence trail")
    st.caption(
        "What was retrieved, from where, and how much of it — before the answer, "
        "because the answer is only as good as this."
    )

    render_route(result)
    render_trace(result, api_base_url)

    st.markdown("---")
    render_answer(result)

    st.markdown("---")
    render_evaluation(result.get("evaluation_summary") or {})

    with st.expander("Output files"):
        files = result.get("output_files") or {}

        if files:
            for label, path in files.items():
                st.code(f"{label}: {path}", language="text")
        else:
            st.caption("None. A refused or replayed run writes no artefacts.")

    with st.expander("Raw API response"):
        st.json(result)


# --------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------


def render_sidebar(api_base_url_default: str) -> tuple[str, str | None, dict | None]:
    with st.sidebar:
        st.header("Backend")

        api_base_url = st.text_input("API base URL", value=api_base_url_default)

        health = None

        try:
            health = call_health(api_base_url)
            mode = health.get("mode", "live")

            if mode == "demo":
                st.info("**Demo mode** — replaying recorded runs.")
            else:
                st.success("Live. All dependencies answering.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"API unreachable: {exc}")

        if health:
            with st.expander("Health detail"):
                st.json(health)

        st.markdown("---")

        scenarios_payload = call_scenarios(api_base_url)
        chosen = None

        if scenarios_payload and scenarios_payload.get("scenarios"):
            scenarios = scenarios_payload["scenarios"]
            demo = scenarios_payload.get("mode") == "demo"

            st.header("Recorded runs" if demo else "Example questions")

            if demo:
                st.caption(
                    f"This deployment replays {len(scenarios)} recorded runs and calls "
                    f"no model. Anything else returns "
                    f"`DEMO_NO_SCENARIO` rather than an invented answer."
                )

            labels = {
                f"{s['id']} — {s['headline']}": s["question"] for s in scenarios
            }

            selection = st.selectbox("Pick one", list(labels))

            detail = next(
                s for s in scenarios if s["question"] == labels[selection]
            )

            st.caption(detail["shows"])

            if detail.get("caveat"):
                st.warning(detail["caveat"])

            if st.button("Use this question"):
                chosen = labels[selection]
        else:
            st.header("Example questions")
            selection = st.selectbox("Pick one", FALLBACK_EXAMPLES)

            if st.button("Use this question"):
                chosen = selection

        return api_base_url, chosen, scenarios_payload


def main() -> None:
    st.set_page_config(
        page_title="Enterprise AI Intelligence Platform",
        page_icon="🧠",
        layout="wide",
    )

    st.title("Enterprise Multi-Agent Knowledge Intelligence Platform")
    st.caption(
        "A question is routed by an LLM planner across PostgreSQL, Neo4j and Qdrant, "
        "answered from what those stores returned, and scored by a separate evaluator. "
        "Every claim below is shown with the evidence behind it."
    )

    api_base_url, chosen, _ = render_sidebar(API_BASE_URL)

    if "query_text" not in st.session_state:
        st.session_state.query_text = FALLBACK_EXAMPLES[0]

    if chosen:
        st.session_state.query_text = chosen

    query = st.text_area(
        "Business question",
        value=st.session_state.query_text,
        height=100,
        key="query_text_area",
    )

    if not st.button("Run", type="primary"):
        return

    if not query.strip():
        st.warning("Enter a question.")
        return

    st.session_state.query_text = query

    with st.spinner("Running..."):
        try:
            started = datetime.now()
            result = call_query(api_base_url, query.strip())
            elapsed = (datetime.now() - started).total_seconds()
        except requests.exceptions.ConnectionError:
            st.error(
                f"Could not reach the API at {api_base_url}. Start it with: "
                "`uvicorn src.api.main:app --port 8000`"
            )
            return
        except requests.exceptions.HTTPError as exc:
            st.error(f"The API returned {exc.response.status_code}.")

            if exc.response.status_code == 401:
                st.caption(
                    "POST /query requires a bearer token. Set API_BEARER_TOKEN in the "
                    "UI's environment."
                )

            try:
                st.json(exc.response.json())
            except ValueError:
                st.write(exc.response.text)

            return
        except Exception as exc:  # noqa: BLE001
            st.error(f"Unexpected error: {exc}")
            return

    st.caption(f"Round trip including network: {elapsed:.2f} s")
    render_response(result, api_base_url)


if __name__ == "__main__":
    main()
