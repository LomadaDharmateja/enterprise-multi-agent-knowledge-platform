# SQL Business Views

## Purpose

This document describes the PostgreSQL business intelligence views created for the Enterprise Multi-Agent Knowledge Intelligence Platform.

The views provide a clean business-facing query layer on top of the normalized ecommerce tables. They are designed to support analytics, executive reporting, future SQL Agent tool calls, and hybrid retrieval workflows.

Instead of requiring downstream agents to manually join raw tables, the views expose commonly needed business concepts in a stable and understandable format.

## Views

### `vw_order_summary`

This view provides one row per order.

It combines order, customer, item, payment, delivery, and review context.

Typical use cases:

* order investigation
* customer support analysis
* delivery delay analysis
* payment reconciliation
* order-level business reporting
* future SQL Agent order queries

### `vw_customer_order_history`

This view provides one row per unique customer profile.

It summarizes customer order activity, lifetime payment value, average order value, review behavior, and late delivery experience.

Typical use cases:

* customer intelligence
* customer segmentation
* customer lifetime value analysis
* support prioritization
* executive customer reporting

### `vw_seller_performance`

This view provides one row per seller.

It summarizes seller revenue, sold items, order count, average review score, review volume, and late delivery orders.

Typical use cases:

* seller performance monitoring
* vendor risk analysis
* seller escalation analysis
* marketplace operations reporting

### `vw_product_performance`

This view provides one row per product.

It summarizes product revenue, order frequency, sold item count, freight value, category information, and review signals.

Typical use cases:

* product intelligence
* category analysis
* product quality investigation
* warranty and complaint analysis
* retrieval metadata for product-linked documents

### `vw_review_intelligence`

This view provides one row per review.

It combines review text, sentiment label, review score, order context, and customer location context.

Typical use cases:

* complaint mining
* sentiment analysis
* support ticket generation
* negative review investigation
* customer experience analytics

### `vw_payment_summary`

This view provides one row per order with payment behavior.

It summarizes payment types, total payment value, number of payment records, and installment patterns.

Typical use cases:

* payment analytics
* fraud investigation simulation
* financial reporting
* customer payment behavior analysis

## Design Principles

The views follow these principles:

* preserve normalized base tables
* avoid direct agent dependency on raw tables
* expose business-friendly query structures
* reduce repeated SQL join logic
* support future SQL Agent tools
* support future executive reporting
* support hybrid retrieval metadata enrichment

## Role in the Final Platform

These views will later be used by:

* SQL Agent
* Supervisor Agent
* Report Generation Agent
* business analytics workflows
* synthetic support ticket generation
* retrieval evaluation datasets
* Streamlit dashboards

The views make the PostgreSQL layer easier to use as an enterprise intelligence backend.
