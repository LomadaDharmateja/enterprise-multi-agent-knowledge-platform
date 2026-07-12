# Hybrid Retrieval Design

## Purpose

The hybrid retrieval layer combines PostgreSQL, Neo4j, and Qdrant to answer enterprise business questions using multiple retrieval strategies.

The objective is to demonstrate why vector-only retrieval is insufficient for enterprise intelligence tasks.

## Retrieval Sources

### PostgreSQL Retrieval

PostgreSQL provides structured and exact business facts.

It is used for:

* order summaries
* customer history
* seller performance
* product performance
* payment summaries
* review intelligence
* aggregate business metrics

PostgreSQL is best when the query requires precise counts, sums, averages, rankings, filters, or structured joins.

### Neo4j Retrieval

Neo4j provides graph-connected reasoning.

It is used for:

* customer to order to product traversal
* seller to ticket to product investigation
* negative review escalation paths
* logistics incident relationships
* warranty claim relationships
* category-policy-guide connections
* multi-hop business reasoning

Neo4j is best when the query asks about relationships, connected entities, paths, or cause-and-effect style investigation.

### Qdrant Retrieval

Qdrant provides semantic document retrieval.

It is used for:

* support ticket search
* customer email search
* logistics incident search
* warranty claim search
* policy document search
* troubleshooting guide search

Qdrant is best when the query is natural language and requires finding semantically similar documents.

## Why Hybrid Retrieval Is Needed

Enterprise questions often require more than one retrieval mode.

Example query:

```text
Find high-risk sellers with negative customer issues and relevant support policies.
```

This requires:

* SQL to rank sellers by performance metrics
* Neo4j to find connected seller-ticket-review-product paths
* Qdrant to retrieve related support tickets and policy documents

A vector-only RAG system may retrieve documents but cannot reliably calculate structured metrics or traverse verified relationships.

A SQL-only system cannot understand unstructured complaints or policy text.

A graph-only system cannot perform semantic document search.

Hybrid retrieval combines all three.

## Initial Retrieval Strategy

The first implementation uses deterministic query routing.

The retriever runs:

1. SQL retrieval for structured business tables and views
2. Graph retrieval for relationship paths
3. Vector retrieval for semantic documents
4. Result packaging into a single JSON response

No LLM is used in this phase.

This keeps the retrieval layer testable, explainable, and production-friendly.

## Future Use

The hybrid retrieval layer will later be used by:

* Supervisor Agent
* SQL Agent
* Graph Agent
* RAG Retrieval Agent
* Report Generation Agent
* evaluation pipelines
* Streamlit interface
* FastAPI backend

## Design Principles

The retrieval layer follows these principles:

* avoid direct raw-table access when business views exist
* use safe predefined SQL and Cypher templates first
* preserve source attribution
* return structured JSON outputs
* keep retrieval independent from LLM generation
* support future query planning and agent routing
* support evaluation before adding agents
