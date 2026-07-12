# Neo4j Graph Schema

## Purpose

Neo4j is used as the graph reasoning layer for the Enterprise Multi-Agent Knowledge Intelligence Platform.

The graph layer represents connected business entities from the ecommerce domain. It supports multi-hop reasoning, relationship traversal, graph-enhanced retrieval, customer intelligence, seller investigation, product issue analysis, and future Graph RAG workflows.

PostgreSQL remains the system of record for structured transactional data. Neo4j is derived from PostgreSQL and optimized for relationship-centric queries.

## Core Graph Nodes

### Customer

Represents a customer profile.

Primary property:

```text
customer_id
```

Additional properties:

```text
customer_unique_id
customer_city
customer_state
customer_zip_code_prefix
```

### Order

Represents an order transaction and lifecycle.

Primary property:

```text
order_id
```

Additional properties:

```text
order_status
approval_status
shipment_status
delivery_status
order_purchase_timestamp
order_approved_at
order_delivered_carrier_date
order_delivered_customer_date
order_estimated_delivery_date
is_late_delivery
delivery_delay_days
total_payment_value
total_item_value
avg_review_score
```

### OrderItem

Represents a line item inside an order.

Primary property:

```text
order_item_key
```

The `order_item_key` is generated as:

```text
order_id + "::" + order_item_id
```

Additional properties:

```text
order_id
order_item_id
price
freight_value
shipping_limit_date
```

### Product

Represents a product catalog item.

Primary property:

```text
product_id
```

Additional properties:

```text
product_category_name
product_category_name_english
product_weight_g
product_length_cm
product_height_cm
product_width_cm
```

### Seller

Represents a marketplace seller or vendor.

Primary property:

```text
seller_id
```

Additional properties:

```text
seller_city
seller_state
seller_zip_code_prefix
```

### Payment

Represents a payment record.

Primary property:

```text
payment_key
```

The `payment_key` is generated as:

```text
order_id + "::" + payment_sequential
```

Additional properties:

```text
order_id
payment_sequential
payment_type
payment_installments
payment_value
```

### Review

Represents a customer review event.

Primary property:

```text
review_key
```

The `review_key` is generated as:

```text
review_id + "::" + order_id
```

Additional properties:

```text
review_id
order_id
review_score
sentiment_label
review_comment_title
review_comment_message
review_creation_date
review_answer_timestamp
```

### Category

Represents a product category.

Primary property:

```text
category_id
```

The `category_id` is based on `product_category_name`.

Additional properties:

```text
product_category_name
product_category_name_english
```

### Region

Represents a geographic business region.

Primary property:

```text
region_id
```

The `region_id` is generated as:

```text
state + "::" + city
```

Additional properties:

```text
city
state
```

The region model is intentionally simple in the first graph version. Zip-code-prefix-level modeling can be added later if needed.

## Graph Relationships

### Customer to Order

```cypher
(:Customer)-[:PLACED]->(:Order)
```

Business meaning:

A customer placed an order.

### Order to OrderItem

```cypher
(:Order)-[:CONTAINS_ITEM]->(:OrderItem)
```

Business meaning:

An order contains one or more line items.

### OrderItem to Product

```cypher
(:OrderItem)-[:REFERENCES_PRODUCT]->(:Product)
```

Business meaning:

An order item refers to a purchased product.

### OrderItem to Seller

```cypher
(:OrderItem)-[:SOLD_BY]->(:Seller)
```

Business meaning:

A seller sold the item in the order.

### Order to Payment

```cypher
(:Order)-[:HAS_PAYMENT]->(:Payment)
```

Business meaning:

An order has one or more payment records.

### Order to Review

```cypher
(:Order)-[:HAS_REVIEW]->(:Review)
```

Business meaning:

An order may have one or more review records.

### Product to Category

```cypher
(:Product)-[:BELONGS_TO_CATEGORY]->(:Category)
```

Business meaning:

A product belongs to a product category.

### Customer to Region

```cypher
(:Customer)-[:LOCATED_IN]->(:Region)
```

Business meaning:

A customer is associated with a city-state region.

### Seller to Region

```cypher
(:Seller)-[:LOCATED_IN]->(:Region)
```

Business meaning:

A seller is associated with a city-state region.

## Initial Graph Design Principles

The graph is derived from validated PostgreSQL data.

The first version focuses on reliable relationships only. Weak or ambiguous relationships are avoided.

Synthetic enterprise artifacts such as tickets, incidents, emails, and policy documents will be added later after the base graph is validated.

The graph uses generated keys for composite-key entities such as OrderItem, Payment, and Review.

Neo4j is not treated as the system of record. PostgreSQL remains the trusted transactional source.

## Future Graph Extensions

Future nodes may include:

```text
Ticket
Incident
Email
PolicyDocument
TroubleshootingGuide
WarrantyClaim
Shipment
Escalation
```

Future relationships may include:

```text
(:Ticket)-[:ABOUT_ORDER]->(:Order)
(:Ticket)-[:MENTIONS_PRODUCT]->(:Product)
(:Ticket)-[:ASSIGNED_TO_SELLER]->(:Seller)
(:Incident)-[:AFFECTS_ORDER]->(:Order)
(:Email)-[:RELATED_TO_TICKET]->(:Ticket)
(:PolicyDocument)-[:APPLIES_TO_CATEGORY]->(:Category)
```

These will be added only after synthetic enterprise data generation.
