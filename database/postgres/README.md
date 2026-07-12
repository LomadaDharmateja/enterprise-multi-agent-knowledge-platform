# PostgreSQL Relational Schema

## Purpose

This directory contains the PostgreSQL schema for the Enterprise Multi-Agent Knowledge Intelligence Platform.

PostgreSQL acts as the structured system of record for the cleaned Olist transactional data.

## Tables

The schema contains the following core tables:

- `customers`
- `sellers`
- `product_category_translations`
- `products`
- `orders`
- `order_items`
- `payments`
- `reviews`

## Schema Design Principles

The schema follows enterprise data engineering principles:

- primary keys are defined explicitly
- foreign-key relationships preserve referential integrity
- composite keys are used where a single column is not sufficient
- timestamps are stored using PostgreSQL timestamp types
- monetary values are stored using numeric types
- indexes are created for common analytical and retrieval access patterns

## Important Modeling Decisions

### Reviews

The `reviews` table uses a composite primary key:

```text
review_id + order_id