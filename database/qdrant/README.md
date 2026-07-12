# Qdrant Vector Database Layer

## Purpose

Qdrant is used as the vector database for the Enterprise Multi-Agent Knowledge Intelligence Platform.

It stores semantic embeddings for enterprise knowledge artifacts such as support tickets, customer emails, logistics incidents, warranty claims, policy documents, and troubleshooting guides.

## Role in the Architecture

PostgreSQL stores structured transactional data.

Neo4j stores graph-connected enterprise relationships.

Qdrant stores semantic vector representations of unstructured and semi-structured enterprise documents.

Together, these systems support hybrid retrieval:

* SQL retrieval for structured business facts
* Neo4j traversal for connected reasoning
* Qdrant vector search for semantic document retrieval

## Collection

The first Qdrant collection is:

```text
enterprise_knowledge
```

## Embedding Model

The initial local embedding model is:

```text
sentence-transformers/all-MiniLM-L6-v2
```

This model produces 384-dimensional embeddings and is suitable for lightweight semantic search experiments.

## Stored Document Types

The collection will contain embeddings for:

* support tickets
* logistics incidents
* customer emails
* warranty claims
* policy documents
* troubleshooting guides

## Metadata Payload

Each Qdrant point will include metadata such as:

* artifact type
* document ID
* customer ID
* order ID
* product ID
* seller ID
* category ID
* region ID
* ticket ID
* issue type
* severity
* source file

This metadata will support filtered semantic search.

## Design Decision

MongoDB is skipped for the current phase.

Synthetic artifacts are stored as JSONL files and then loaded into Neo4j and Qdrant. MongoDB can be added later if operational document storage is required.
