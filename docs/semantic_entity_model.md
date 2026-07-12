# Semantic Entity Model

## Purpose

This document defines the enterprise semantic entity model for the Knowledge Intelligence Platform. The goal is to convert the cleaned Olist e-commerce datasets into a business-oriented data model that can support relational querying, graph reasoning, hybrid retrieval, synthetic enterprise documents, and multi-agent workflows.

The semantic model acts as a bridge between the cleaned canonical data layer and the future database architecture.

## Core Enterprise Entities

### Customer

The Customer entity represents an individual customer profile in the marketplace.

Source dataset: `customers_cleaned.csv`

Primary key: `customer_id`

A customer can place one or more orders. In this dataset, each customer record is linked to order activity through the `customer_id` field.

### Order

The Order entity represents a business transaction and its lifecycle.

Source dataset: `orders_cleaned.csv`

Primary key: `order_id`

An order captures the commercial event between a customer, one or more products, sellers, payments, delivery status, and customer review activity.

Orders are central to the platform because most downstream intelligence tasks, such as customer support, logistics analysis, complaint investigation, and executive reporting, depend on the order lifecycle.

### Product

The Product entity represents a sellable catalog item.

Source dataset: `products_cleaned.csv`

Primary key: `product_id`

A product can appear in one or more order items. Product metadata is important for product-level analytics, issue classification, customer complaints, and troubleshooting documents.

### Seller

The Seller entity represents a marketplace seller or vendor.

Source dataset: `sellers_cleaned.csv`

Primary key: `seller_id`

A seller can sell multiple products across multiple orders. Seller information is useful for vendor performance monitoring, delivery issue analysis, complaint escalation, and business reporting.

### Order Item

The Order Item entity represents a line item inside an order.

Source dataset: `order_items_cleaned.csv`

Primary key: `order_id + order_item_id`

An order item connects an order to a product and a seller. This entity is necessary because an order may contain multiple products and multiple seller relationships.

### Payment

The Payment entity represents a payment event associated with an order.

Source dataset: `payments_cleaned.csv`

Primary key: `order_id + payment_sequential`

An order can have one or more payment records. This supports analysis of payment behavior, installment patterns, payment method distribution, and order-level financial activity.

### Review

The Review entity represents customer feedback and satisfaction signals.

Source dataset: `reviews_cleaned.csv`

Primary key: `review_id + order_id`

The review entity uses a composite key because `review_id` alone is not fully unique in the cleaned data. Reviews are preserved because they provide important customer satisfaction information and can later be connected to support tickets, complaints, and sentiment analysis.

## Validated Enterprise Relationships

### Customer Places Order

Relationship: `Customer -> Order`

Key: `customer_id`

Cardinality: one-to-many

Business meaning: A customer can place one or more orders.

### Order Contains Item

Relationship: `Order -> Order Item`

Key: `order_id`

Cardinality: one-to-many

Business meaning: An order can contain one or more line items.

### Order Item References Product

Relationship: `Order Item -> Product`

Key: `product_id`

Cardinality: many-to-one

Business meaning: Each order item refers to a purchased product.

### Order Item Sold By Seller

Relationship: `Order Item -> Seller`

Key: `seller_id`

Cardinality: many-to-one

Business meaning: Each order item is associated with the seller that sold the product.

### Order Has Payment

Relationship: `Order -> Payment`

Key: `order_id`

Cardinality: one-to-many

Business meaning: An order can have one or more payment records.

### Order Has Review

Relationship: `Order -> Review`

Key: `order_id`

Cardinality: one-to-many-or-zero

Business meaning: An order may have no review, one review, or multiple review records.

## Modeling Decisions

The review entity is modeled using a composite key because `review_id` alone is not fully unique. Instead of deleting duplicate review records, the data model preserves them and treats review records as order-linked feedback events.

The geolocation dataset is not yet modeled as a core transactional entity. It will be introduced later as part of region entity engineering because geolocation data is based on zip-code prefixes and regional approximations rather than direct transactional relationships.

The current model focuses only on strongly validated transactional relationships. This avoids introducing weak or ambiguous links too early in the architecture.

## Role in the Final Platform

This semantic entity model will guide:

* PostgreSQL table design
* SQL foreign-key relationships
* Neo4j node and edge construction
* MongoDB document reference design
* Qdrant metadata filtering
* synthetic support ticket generation
* Graph RAG relationship traversal
* multi-agent tool design
* retrieval evaluation datasets

This step ensures that future AI components operate on a reliable enterprise data foundation rather than disconnected raw files.
