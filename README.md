## Enterprise Multi-Agent Knowledge Intelligence Platform

An enterprise-grade agentic AI platform that combines **LLM-based planning**, **SQL analytics**, **graph reasoning**, **vector search**, **grounded answer generation**, **answer evaluation**, **observability**, **FastAPI**, **Streamlit**, and **Docker deployment**.

The project demonstrates how an enterprise AI assistant can answer complex business questions by using multiple tools instead of relying only on a single vector database or direct LLM response.

---

## 1. Problem Statement

Modern enterprises store knowledge across many disconnected systems.

For example, an e-commerce company may have:

- Customer and order data in relational databases
- Product, seller, and review relationships in graph-like structures
- Support tickets, warranty claims, policies, and troubleshooting documents in unstructured text files
- Operational incidents spread across different business functions

When a business user asks a question such as:

> “Which sellers are linked to negative customer complaints, warranty issues, and relevant support policies?”

a simple chatbot or basic RAG system is not enough.

The answer requires multiple types of reasoning:

- Structured analytics from SQL tables
- Relationship reasoning across customers, orders, products, sellers, and tickets
- Semantic search over support documents, warranty claims, and policy documents
- LLM reasoning to combine evidence into a clear business answer
- Grounding checks to make sure the LLM does not hallucinate

---

## 2. What This Project Solves

This project solves the problem of answering complex enterprise business questions using a controlled multi-agent AI workflow.

Instead of allowing the LLM to answer directly from memory, the system forces the LLM to follow an evidence-based process:

1. Understand the business question.
2. Plan which tools are needed.
3. Retrieve evidence from SQL, graph, and vector systems.
4. Build a grounded context.
5. Generate a business answer from the retrieved evidence.
6. Evaluate the answer for grounding, completeness, and unsupported claims.
7. Return the final response through an API and UI.

This makes the system more reliable, explainable, and enterprise-ready than a basic chatbot.

---

## 3. High-Level Solution

The platform uses a multi-agent architecture.

```text
User Query
   ↓
Gemini Query Planner Agent
   ↓
Hybrid Retrieval Tools
   ├── PostgreSQL: structured business facts
   ├── Neo4j: connected business relationships
   └── Qdrant: semantic enterprise document retrieval
   ↓
Retrieval Context Builder
   ↓
Gemini Grounded Answer Agent
   ↓
Gemini Evaluation Agent
   ↓
FastAPI Response
   ↓
Streamlit UI
```

The key idea is:

> The LLM does not directly answer the user.
> The LLM first plans tool usage, then answers only from retrieved enterprise evidence, and another LLM agent evaluates the answer.

---

## 4. What I Built

This project includes a complete working enterprise AI pipeline.

### Main components

* Data cleaning and preprocessing pipeline
* PostgreSQL relational database layer
* SQL business views for analytics
* Neo4j graph database layer
* Synthetic enterprise artifact generation
* Qdrant vector database ingestion
* Hybrid retrieval engine
* Retrieval context builder
* Gemini query planner agent
* Gemini grounded answer generator
* Gemini answer evaluation agent
* LangGraph orchestration workflow
* FastAPI backend
* Streamlit frontend
* Observability logs and metrics
* Docker deployment setup
* End-to-end validation scripts

---

## 5. Why This Is More Than Basic RAG

A basic RAG system usually works like this:

```text
User query → vector search → LLM answer
```

That approach is limited because many enterprise questions require structured and relational evidence.

This project uses a stronger pattern:

```text
User query
   ↓
LLM planner
   ↓
SQL + graph + vector retrieval
   ↓
Grounded answer generation
   ↓
LLM answer evaluation
   ↓
Observable final response
```

This allows the system to answer questions that require:

* Business metrics
* Entity relationships
* Customer complaint history
* Seller-product-ticket connections
* Warranty and policy evidence
* Delivery and logistics context
* Evaluation of answer quality

---

## 6. Example Business Questions

The system can answer questions such as:

```text
Find sellers with negative customer complaints, warranty issues, and relevant support policies
```

```text
Investigate late delivery logistics incidents by customer region and find troubleshooting guidance
```

```text
Find payment questions, refund policies, and customer support cases
```

```text
Investigate product quality complaints and find troubleshooting procedures
```

```text
Show seller ticket paths for negative complaints and product issues
```

---

## 7. Example Agentic Workflow

For this query:

```text
Find sellers with negative customer complaints, warranty issues, and relevant support policies
```

the Gemini planner selects:

```text
SQL route: seller_performance
Graph route: warranty_product_seller_paths
Vector groups: support_tickets, warranty_claims, policy_documents
```

The system then retrieves:

* Seller performance metrics from PostgreSQL
* Warranty-product-seller relationships from Neo4j
* Support tickets, warranty claims, and policy documents from Qdrant

The answer generator then produces a grounded business response.

The evaluator checks the answer and returns scores such as:

```text
Evaluation status: PASS
Grounding score: 5
Completeness score: 5
Business readiness score: 5
Unsupported claims: 0
```

---

## 8. Architecture

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
   ├── PostgreSQL
   │   └── Structured business views
   ├── Neo4j
   │   └── Connected business paths
   └── Qdrant
       └── Semantic enterprise document retrieval
   ↓
Retrieval Context Builder
   ↓
Gemini Grounded Answer Agent
   ↓
Gemini Evaluation Agent
   ↓
Final Business Answer
   ↓
Observability Logs and Metrics
```

---

## 9. Technology Stack

| Layer                        | Technology                             |
| ---------------------------- | -------------------------------------- |
| Programming language         | Python                                 |
| LLM                          | Gemini                                 |
| Agent workflow orchestration | LangGraph                              |
| API backend                  | FastAPI                                |
| Frontend UI                  | Streamlit                              |
| Relational database          | PostgreSQL                             |
| Graph database               | Neo4j                                  |
| Vector database              | Qdrant                                 |
| Embedding model              | sentence-transformers/all-MiniLM-L6-v2 |
| Containerization             | Docker and Docker Compose              |
| Observability                | Local JSONL logs and CSV metrics       |

---

## 10. Data Used

The project uses the Brazilian Olist e-commerce dataset as the structured business foundation.

Structured data includes:

* Customers
* Orders
* Order items
* Products
* Sellers
* Payments
* Reviews
* Product categories
* Region information

To simulate enterprise knowledge sources, synthetic business artifacts were generated:

* Support tickets
* Logistics incidents
* Customer emails
* Warranty claims
* Policy documents
* Troubleshooting guides

These artifacts allow the system to behave like an internal enterprise AI assistant.

---

## 11. Storage Layers

### PostgreSQL

PostgreSQL stores structured e-commerce business data.

Business views include:

* Seller performance
* Product performance
* Review intelligence
* Payment summary
* Order summary
* Customer order history

### Neo4j

Neo4j stores connected business relationships.

Example paths:

```text
Customer → Order → Product → Seller
```

```text
Support Ticket → Product → Seller
```

```text
Warranty Claim → Product → Seller
```

```text
Logistics Incident → Region → Order
```

```text
Category → Policy → Troubleshooting Guide
```

### Qdrant

Qdrant stores vector embeddings for enterprise artifacts.

Document groups include:

* support_tickets
* logistics_incidents
* customer_emails
* warranty_claims
* policy_documents
* troubleshooting_guides

---

## 12. AI Agents

### 1. Gemini Query Planner Agent

The planner receives the user query and decides which retrieval tools should be used.

It selects:

* SQL intent
* Graph intent
* Vector artifact groups

Example:

```json
{
  "sql_intent": "seller_performance",
  "graph_intent": "warranty_product_seller_paths",
  "vector_artifact_groups": [
    "support_tickets",
    "warranty_claims",
    "policy_documents"
  ]
}
```

### 2. Gemini Grounded Answer Agent

This agent receives only the retrieved context and generates a business answer.

It is instructed to use:

* SQL evidence for structured facts
* Graph evidence for connected relationships
* Document evidence for policies, tickets, incidents, emails, and claims

### 3. Gemini Evaluation Agent

This agent evaluates the generated answer.

It checks:

* Whether SQL evidence was used
* Whether graph evidence was used
* Whether document evidence was used
* Whether the answer contains unsupported claims
* Whether limitations are mentioned
* Whether the response is business-ready

---

## 13. Observability

The workflow includes local observability.

For each run, the system records:

* Run ID
* Component name
* Operation name
* Start and end events
* Status
* Duration
* Output metadata
* Error details if a node fails

Observed components:

```text
gemini_planner_agent
hybrid_retrieval_tools
gemini_answer_agent
gemini_evaluation_agent
```

Observability files:

```text
reports/observability/workflow_events.jsonl
reports/observability/workflow_metrics.csv
```

Generate a report:

```powershell
python src/observability/observability_report.py
```

---

## 14. User Interface

The Streamlit UI allows users to:

* Enter a business question
* Select example queries
* Check FastAPI health
* Run the agentic workflow
* View selected SQL, graph, and vector routes
* View grounded business answers
* View evaluation scores
* Inspect unsupported claims
* View output files

The UI shows:

```text
Workflow Status
Answer Provider
Answer Model
Answer Length
Evaluation Status
Grounding Score
Completeness Score
Business Readiness Score
Unsupported Claims
SQL Route
Graph Route
Vector Groups
Grounded Answer
```

---

## 15. API

The FastAPI backend exposes:

```text
GET /
GET /health
POST /query
GET /docs
```

Example request:

```json
{
  "query": "Find sellers with negative customer complaints, warranty issues, and relevant support policies"
}
```

The API returns:

* Workflow status
* Answer provider and model
* Source summary
* Evaluation summary
* Answer text
* Output file paths

---

## 16. Project Structure

```text
enterprise_ai/
├── configs/
│   └── relationship_contract.yaml
├── data/
│   ├── raw/
│   ├── processed/
│   └── synthetic/
├── database/
│   ├── postgres/
│   ├── neo4j/
│   └── qdrant/
├── docs/
├── reports/
├── src/
│   ├── api/
│   ├── data_engineering/
│   ├── database/
│   ├── evaluation/
│   ├── generation/
│   ├── graph/
│   ├── observability/
│   ├── orchestration/
│   ├── planning/
│   ├── retrieval/
│   ├── synthetic/
│   ├── ui/
│   └── vector/
├── Dockerfile.api
├── Dockerfile.ui
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## 17. Environment Variables

Create a `.env` file in the project root.

Use `.env.example` as a template.

```env
POSTGRES_DB=enterprise_ai
POSTGRES_USER=enterprise_user
POSTGRES_PASSWORD=enterprise_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=enterprise_neo4j_password
NEO4J_HTTP_PORT=7474
NEO4J_BOLT_PORT=7687

QDRANT_HOST=localhost
QDRANT_HTTP_PORT=6333
QDRANT_GRPC_PORT=6334
QDRANT_COLLECTION=enterprise_knowledge

EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIMENSION=384

GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.1-flash-lite

OBSERVABILITY_DIR=reports/observability
```

Do not commit the real `.env` file.

---

## 18. How to Run Locally

### 1. Activate virtual environment

```powershell
.venv\Scripts\activate
```

### 2. Run FastAPI

```powershell
uvicorn src.api.main:app --reload --port 8000
```

### 3. Run Streamlit

Open another terminal:

```powershell
streamlit run src/ui/streamlit_app.py
```

### 4. Open the UI

```text
http://localhost:8501
```

---

## 19. Docker Deployment

The project has been validated in full local Docker runtime mode.

The Docker stack includes:

* PostgreSQL
* Neo4j
* Qdrant
* FastAPI
* Streamlit

Validate Docker Compose:

```powershell
docker compose config
```

Build API and UI images:

```powershell
docker compose build api ui
```

Start the full stack:

```powershell
docker compose up -d
```

Check containers:

```powershell
docker compose ps
```

Open Streamlit:

```text
http://localhost:8501
```

FastAPI runs at:

```text
http://localhost:8000
```

Cloud deployment is not yet implemented.

---

## 20. Data Loading Order for Fresh Docker Containers

For a fresh Docker deployment, load data in this order.

### PostgreSQL

```powershell
docker exec -it enterprise_ai_postgres_deploy psql -U enterprise_user -d enterprise_ai -c "CREATE SCHEMA IF NOT EXISTS ecommerce;"
python src/database/postgres_loader.py
Get-Content database/postgres/views.sql | docker exec -i enterprise_ai_postgres_deploy psql -U enterprise_user -d enterprise_ai
Get-Content database/postgres/indexes.sql | docker exec -i enterprise_ai_postgres_deploy psql -U enterprise_user -d enterprise_ai
python src/database/postgres_validator.py
python src/database/sql_view_validator.py
```

### Neo4j

```powershell
Get-Content database/neo4j/constraints.cypher | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
Get-Content database/neo4j/indexes.cypher | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
python src/graph/neo4j_loader.py
python src/graph/neo4j_validator.py
```

### Synthetic Neo4j Graph

```powershell
Get-Content database/neo4j/synthetic_constraints.cypher | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
Get-Content database/neo4j/synthetic_indexes.cypher | docker exec -i enterprise_ai_neo4j_deploy cypher-shell -u neo4j -p enterprise_neo4j_password
python src/graph/synthetic_neo4j_loader.py
python src/graph/synthetic_graph_validator.py
```

### Qdrant

```powershell
python src/vector/qdrant_ingest.py
python src/vector/qdrant_validator.py
```

---

## 21. Validation Commands

Run these validators to confirm the system is working.

```powershell
python src/database/postgres_validator.py
python src/database/sql_view_validator.py
python src/graph/neo4j_validator.py
python src/graph/synthetic_graph_validator.py
python src/vector/qdrant_validator.py
python src/retrieval/hybrid_retrieval_validator.py
python src/retrieval/retrieval_context_validator.py
python src/generation/answer_generation_validator.py
python src/orchestration/agentic_workflow_validator.py
python src/api/api_validator.py
```

Expected result:

```text
Overall status: PASS
```

---

## 22. Final Validation Status

| Component                            | Status |
| ------------------------------------ | ------ |
| PostgreSQL schema and loading        | PASS   |
| SQL business views                   | PASS   |
| Neo4j graph loading                  | PASS   |
| Synthetic enterprise data generation | PASS   |
| Synthetic Neo4j graph loading        | PASS   |
| Qdrant vector ingestion              | PASS   |
| Hybrid retrieval                     | PASS   |
| Retrieval context builder            | PASS   |
| Gemini query planner agent           | PASS   |
| Gemini grounded answer generation    | PASS   |
| Gemini answer evaluation agent       | PASS   |
| LangGraph agentic workflow           | PASS   |
| FastAPI backend                      | PASS   |
| Streamlit UI                         | PASS   |
| Local observability                  | PASS   |
| Docker Compose configuration         | PASS   |
| API Docker image build               | PASS   |
| UI Docker image build                | PASS   |
| Full local Docker runtime            | PASS   |

---

## 23. What Makes This Project Strong

This project demonstrates several production-style AI engineering concepts:

* Agentic AI workflow orchestration
* LLM-based tool planning
* Hybrid SQL, graph, and vector retrieval
* Grounded answer generation
* LLM-based answer evaluation
* Observability and run tracing
* API and UI serving layers
* Docker-based deployment
* End-to-end validation
* Modular software architecture

It is designed to show how enterprise AI systems can be built beyond simple chatbots.

---

## 23b. Known Limitations

Measured during the M3 evaluation (83 held-out questions). Stated here rather than in a
footnote because a stated limitation is cheaper than a false claim.

**Population counts and aggregate statistics are not supported.** The system answers
questions about specific entities and relationships. Every SQL template is a
`SELECT ... ORDER BY ... LIMIT :limit` over a view; none computes `COUNT`, `AVG`,
`MEDIAN` or `SUM` across a population. So "how many orders were cancelled in total?",
"what is the average review score across all delivered orders?" and "which state has
the most sellers?" have no route that can answer them.

The system does not guess when asked one of these. Measured behaviour on
`"What is the total number of orders?"`:

> "The provided records are insufficient to determine the total number of orders. The
> SQL evidence returned a sample of 10 specific order records, but it does not contain
> a count or an aggregate summary of the total order volume in the database."

That is the correct outcome for an unsupported question type, and it is a deliberate
scope boundary, not a defect awaiting a patch.

**Some document fields never reach the embedded text.** `qdrant_ingest.build_document_text()`
rebuilds each document from selected top-level keys instead of embedding the
generator's authored `document_text`. Fields outside that selection -- `root_cause` on
support tickets, `procedure_steps` on troubleshooting guides -- are absent from the
retrieved text even when the correct document is retrieved. Tracked as F-10; see
`docs/M3_FINDINGS.md`.

## 24. Future Improvements

Possible future enhancements:

* Cloud deployment
* Authentication and role-based access
* Streaming responses
* Human approval workflow
* More advanced observability dashboard
* CI/CD pipeline
* Automated data reload jobs
* Feedback loop for answer quality improvement
* Support for additional enterprise data sources
* Multi-user session management

---

## 25. Repository Safety

The repository excludes:

* `.env`
* `.venv`
* generated reports
* raw data
* processed data
* synthetic generated data
* local cache files
* model cache files

Only source code, configuration templates, database scripts, Docker files, and documentation are committed.

```
