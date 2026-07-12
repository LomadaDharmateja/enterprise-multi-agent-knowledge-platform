# Deployment Guide

## Overview

This project supports local development execution and local Docker-based deployment.

The deployment stack includes:

- PostgreSQL
- Neo4j
- Qdrant
- FastAPI backend
- Streamlit frontend

The system also uses Gemini for:

- Query planning
- Grounded answer generation
- Answer evaluation

---

## Deployment Status

Current status:

| Component | Status |
|---|---|
| Docker Compose configuration | PASS |
| API Docker image build | PASS |
| UI Docker image build | PASS |
| Local development execution | PASS |
| Full Docker runtime with data reload | Pending final runtime test |
| Cloud deployment | Not yet deployed |

---

## Environment Setup

Create a `.env` file in the project root.

Use `.env.example` as the template.

Required values:

```env
POSTGRES_DB=enterprise_ai
POSTGRES_USER=enterprise_user
POSTGRES_PASSWORD=enterprise_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=enterprise_neo4j_password

QDRANT_HOST=localhost
QDRANT_HTTP_PORT=6333
QDRANT_GRPC_PORT=6334
QDRANT_COLLECTION=enterprise_knowledge

EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIMENSION=384

GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.1-flash-lite

OBSERVABILITY_DIR=reports/observability