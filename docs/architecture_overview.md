# Architecture Overview

## System Purpose

The Enterprise Multi-Agent Knowledge Intelligence Platform is designed to answer complex business questions using structured data, graph relationships, semantic document retrieval, and LLM-based reasoning.

The system does not allow the LLM to answer directly from memory. Instead, it follows a controlled workflow:

1. Plan the required tools.
2. Retrieve evidence.
3. Build grounded context.
4. Generate an answer.
5. Evaluate the answer.
6. Return the final response through API and UI.

---

## Architecture Diagram

```text
Streamlit UI
   ↓
FastAPI Backend
   ↓
LangGraph Agentic Workflow
   ↓
Gemini Query Planner Agent
   ↓
Hybrid Retrieval Layer
   ├── PostgreSQL: structured business views
   ├── Neo4j: connected business relationships
   └── Qdrant: semantic enterprise document retrieval
   ↓
Retrieval Context Builder
   ↓
Gemini Grounded Answer Agent
   ↓
Gemini Evaluation Agent
   ↓
Final Business Response
   ↓
Observability Logs and Metrics



Main Components
1. Streamlit UI

The UI allows users to submit business questions and inspect:

Workflow status
Gemini model used
SQL route
Graph route
Vector document groups
Grounded answer
Evaluation scores
Unsupported claims
Output files
2. FastAPI Backend

The API exposes the agentic workflow through:

GET /
GET /health
POST /query

The /query endpoint receives a natural-language business question and returns a grounded enterprise answer with evaluation metadata.

3. LangGraph Workflow

LangGraph orchestrates the multi-step workflow.

Workflow nodes:

Gemini query planner
Retrieval context builder
Gemini answer generator
Gemini answer evaluator
Final response packager
4. Gemini Query Planner Agent

The planner selects:

SQL intent
Graph intent
Vector artifact groups

This converts a user question into an executable retrieval route.

5. PostgreSQL Layer

PostgreSQL stores structured business data and exposes business views such as:

Seller performance
Product performance
Review intelligence
Payment summary
Order summary
Customer order history
6. Neo4j Layer

Neo4j stores connected business relationships.

Examples:

Customer → Order → Product → Seller
Support Ticket → Product → Seller
Warranty Claim → Product → Seller
Logistics Incident → Region → Order
Category → Policy → Troubleshooting Guide
7. Qdrant Layer

Qdrant stores vector embeddings of synthetic enterprise artifacts:

Support tickets
Logistics incidents
Customer emails
Warranty claims
Policy documents
Troubleshooting guides
8. Retrieval Context Builder

The context builder converts raw SQL, graph, and vector results into an answer-ready evidence package.

9. Gemini Grounded Answer Agent

The answer agent generates a business answer using only the retrieved context.

10. Gemini Evaluation Agent

The evaluator checks:

SQL evidence usage
Graph evidence usage
Document evidence usage
Unsupported claims
Limitations
Grounding score
Completeness score
Business readiness score
11. Observability

Local observability records:

Run ID
Node start/end events
Component duration
Status
Errors
Output metadata

Files:

reports/observability/workflow_events.jsonl
reports/observability/workflow_metrics.csv
Deployment Modes
Development Mode

Infrastructure runs in Docker, while FastAPI and Streamlit run locally from the Python virtual environment.

Full Local Docker Mode

The complete stack runs in Docker:

PostgreSQL
Neo4j
Qdrant
FastAPI
Streamlit

This mode has been validated successfully.

Enterprise AI Pattern

This project demonstrates an enterprise-grade pattern:

LLM planner → tool execution → grounded generation → LLM evaluation → observable response

This is more robust than a basic RAG chatbot because the system combines:

Structured analytics
Graph reasoning
Semantic retrieval
LLM planning
LLM evaluation
Observability
API/UI serving