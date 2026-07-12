# Neo4j Graph Layer

## Purpose

This directory contains the Neo4j graph schema for the Enterprise Multi-Agent Knowledge Intelligence Platform.

Neo4j is used for relationship-centric reasoning and Graph RAG workflows.

## PostgreSQL vs Neo4j Responsibility

PostgreSQL remains the structured system of record.

Neo4j is a derived graph layer built from PostgreSQL data. It is optimized for relationship traversal and connected reasoning.

## Files

```text
graph_schema.md
constraints.cypher
indexes.cypher