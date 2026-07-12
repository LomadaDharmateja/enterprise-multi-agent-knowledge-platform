# Agentic Workflow Architecture

## Overview

This document describes the LangGraph-based agentic workflow used in the Enterprise Multi-Agent Knowledge Intelligence Platform.

The workflow combines:

- Gemini query planning
- Hybrid retrieval from PostgreSQL, Neo4j, and Qdrant
- Retrieval context building
- Gemini grounded answer generation
- Gemini answer evaluation
- Local observability logging

The system is designed so that the LLM does not answer directly from memory. Instead, it plans tool usage, retrieves enterprise evidence, generates an answer from that evidence, and evaluates the answer before returning it to the user.

---

## High-Level Flow

```text
User Query
   ↓
Gemini Query Planner Agent
   ↓
Hybrid Retrieval Tools
   ├── PostgreSQL structured business retrieval
   ├── Neo4j graph path retrieval
   └── Qdrant semantic document retrieval
   ↓
Retrieval Context Builder
   ↓
Gemini Grounded Answer Agent
   ↓
Gemini Evaluation Agent
   ↓
Final Response
   ↓
Observability Logs and Metrics
```


## LangGraph Nodes

The workflow is implemented as a LangGraph state graph.

### 1. Query Planner Node

The query planner node uses Gemini to inspect the user query and decide which retrieval routes should be executed.

It selects:

* SQL intent
* Graph intent
* Vector artifact groups

Example output:

```json
{
  "planner": "gemini_query_planner",
  "planning_mode": "llm_tool_routing",
  "sql_intent": "seller_performance",
  "graph_intent": "warranty_product_seller_paths",
  "vector_artifact_groups": [
    "support_tickets",
    "warranty_claims",
    "policy_documents"
  ],
  "reasoning": "The query asks about sellers, complaints, warranty issues, and policies."
}
```

### 2. Retrieval Context Builder Node

This node executes the selected retrieval tools and builds an answer-ready context.

It gathers evidence from:

* PostgreSQL business views
* Neo4j graph relationships
* Qdrant semantic document retrieval

The output includes:

* SQL evidence
* Graph evidence
* Document evidence
* Entity identifiers
* Recommended next actions
* Source summary

### 3. Gemini Answer Generation Node

The answer generation node sends the retrieval context to Gemini.

Gemini is instructed to answer only from the provided context and to organize the response as a grounded business answer.

The generated answer includes:

* Business question
* Executive answer
* Evidence used
* Recommended next actions
* Limitations

### 4. Gemini Evaluation Node

The evaluation node uses Gemini as a separate evaluator agent.

It checks:

* Whether SQL evidence was used
* Whether graph evidence was used
* Whether document evidence was used
* Whether unsupported claims exist
* Whether limitations are mentioned
* Whether the answer is business-ready

The evaluator returns scores for:

* Grounding
* Completeness
* Business readiness

### 5. Final Response Node

The final response node packages:

* Workflow status
* Query
* Answer provider and model
* Answer length
* Source summary
* Evaluation summary
* Output files
* Full answer preview
* Observability file paths

---

## Workflow State

The workflow state contains:

```text
run_id
query
planned_route
retrieval_context
answer_result
evaluation_result
final_response
raw_retrieval_path
retrieval_context_path
answer_json_path
answer_md_path
prompt_path
evaluation_json_path
```

The `run_id` is used for observability tracing.

---

## Retrieval Routes

### SQL Intents

Supported SQL intents:

* seller_performance
* product_performance
* review_intelligence
* payment_summary
* customer_history
* order_summary

### Graph Intents

Supported graph intents:

* warranty_product_seller_paths
* logistics_region_paths
* category_policy_guide_paths
* seller_ticket_product_paths
* customer_ticket_order_product_paths

### Vector Artifact Groups

Supported Qdrant artifact groups:

* support_tickets
* logistics_incidents
* customer_emails
* warranty_claims
* policy_documents
* troubleshooting_guides

---

## Example Query

```text
Find sellers with negative customer complaints, warranty issues, and relevant support policies
```

### Planner Route

```text
SQL intent: seller_performance
Graph intent: warranty_product_seller_paths
Vector groups: support_tickets, warranty_claims, policy_documents
```

### Evaluation Result

```text
Evaluation status: PASS
Grounding score: 5
Completeness score: 5
Business readiness score: 5
Unsupported claims: 0
Deterministic validation warnings: 0
```

---

## Observability

The workflow writes local observability artifacts to:

```text
reports/observability/workflow_events.jsonl
reports/observability/workflow_metrics.csv
```

Tracked components:

* gemini_planner_agent
* hybrid_retrieval_tools
* gemini_answer_agent
* gemini_evaluation_agent

Each traced span records:

* Run ID
* Component
* Operation
* Start event
* End event
* Status
* Duration
* Output metadata
* Error information if a node fails

---

## Validation

The workflow is validated using:

```powershell
python src/orchestration/agentic_workflow_validator.py
```

The validator checks:

* Workflow status
* Gemini answer provider
* Gemini model
* Routing correctness
* Answer length
* Evaluation status
* Grounding score
* Completeness score
* Business readiness score
* Unsupported claims
* Required output files

---

## Design Rationale

This architecture is intentionally more robust than a basic RAG chatbot.

A simple RAG system usually retrieves documents and sends them to an LLM. This platform instead uses:

1. LLM-based query planning
2. Structured SQL evidence
3. Graph relationship evidence
4. Semantic document evidence
5. Grounded answer generation
6. LLM-based answer evaluation
7. Local observability

This makes the system more suitable for enterprise decision-support and operational intelligence use cases.

````

