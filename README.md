# Enterprise Multi-Agent Knowledge Intelligence Platform

An enterprise-grade AI platform that combines agentic workflow orchestration, hybrid retrieval, graph reasoning, vector search, and LLM-based answer generation for business intelligence over structured, connected, and unstructured enterprise data.

This project demonstrates how a production-style enterprise AI assistant can answer complex business questions by planning retrieval routes, executing multiple data tools, generating grounded answers, and evaluating answer quality before returning the final response.

---

## 1. Project Overview

This platform is built as an enterprise knowledge intelligence system over an e-commerce business dataset enriched with synthetic enterprise artifacts such as support tickets, logistics incidents, warranty claims, customer emails, policy documents, and troubleshooting guides.

The system can answer questions such as:

- Which sellers are linked to negative customer complaints and warranty issues?
- Which regions are affected by late delivery logistics incidents?
- Which support policies are relevant for refund or warranty cases?
- Which products have quality complaints and related troubleshooting procedures?
- Which seller-ticket-product paths indicate operational risk?

The goal is not only to retrieve documents, but to combine structured business facts, graph-connected relationships, semantic document evidence, and LLM reasoning into one grounded business answer.

---

## 2. Key Capabilities

- Gemini-based query planning agent
- Hybrid retrieval over PostgreSQL, Neo4j, and Qdrant
- LangGraph-based agentic workflow orchestration
- Grounded Gemini answer generation
- Gemini-based answer evaluation and grounding check
- FastAPI backend
- Streamlit user interface
- Validation scripts for every major system layer
- Synthetic enterprise data generation
- SQL business views and graph relationship modeling
- Vector search over enterprise artifacts

---

## 3. High-Level Architecture

```text
Streamlit UI
   ↓
FastAPI Backend
   ↓
LangGraph Agentic Workflow
   ↓
Gemini Query Planner Agent
   ↓
Hybrid Retrieval Tools
   ├── PostgreSQL: structured business facts
   ├── Neo4j: graph-connected entity reasoning
   └── Qdrant: semantic document retrieval
   ↓
Retrieval Context Builder
   ↓
Gemini Grounded Answer Agent
   ↓
Gemini Evaluation Agent
   ↓
Final Business Answer