# Synthetic Enterprise Data Generation

## Purpose

The Olist dataset provides structured ecommerce transaction data such as customers, orders, products, sellers, payments, and reviews. However, an enterprise AI knowledge platform also requires operational and knowledge-based artifacts such as support tickets, customer emails, logistics incidents, warranty claims, policy documents, and troubleshooting guides.

This synthetic data generation stage creates realistic enterprise artifacts that are semantically linked to the validated transactional backbone.

The generated artifacts are not random isolated documents. Every operational record is connected to real IDs from PostgreSQL, such as `customer_id`, `order_id`, `product_id`, `seller_id`, `review_id`, `category_id`, and region information.

## Why Synthetic Data Is Needed

The platform is intended to simulate an internal enterprise AI system. Such systems usually reason across multiple types of information:

* structured transactional data
* customer support records
* complaint emails
* incident logs
* warranty claims
* policy documents
* troubleshooting guides
* executive summaries

The original Olist dataset does not contain these enterprise operational artifacts. Therefore, synthetic artifacts are generated to extend the dataset into a realistic enterprise knowledge environment.

## MongoDB Decision

MongoDB is intentionally skipped for the current implementation phase.

Instead, synthetic operational artifacts are stored as JSONL files first. This keeps the architecture simpler while still preserving the ability to load the same artifacts later into:

* Neo4j as graph nodes and relationships
* Qdrant as vector documents
* PostgreSQL if structured reporting is needed
* MongoDB later if operational document storage is reintroduced

This approach keeps the system modular and avoids unnecessary complexity too early.

## Generated Artifacts

### Support Tickets

Support tickets represent customer service cases linked to real orders, products, sellers, reviews, and delivery information.

Typical issue types include:

* delayed delivery
* damaged item
* product quality complaint
* missing item
* payment question
* negative review escalation

### Logistics Incidents

Logistics incidents represent operational delivery problems linked to late orders and affected sellers or customer regions.

Typical incident types include:

* carrier delay
* regional delivery bottleneck
* warehouse handoff issue
* seller dispatch delay
* address routing issue

### Customer Emails

Customer emails simulate natural communication records related to support tickets.

They are useful for future semantic retrieval because they contain less structured language than tickets.

### Warranty Claims

Warranty claims represent product-quality and post-delivery escalation cases.

They are linked to real orders, products, categories, sellers, and review signals.

### Policy Documents

Policy documents describe enterprise rules for refunds, delivery delays, warranty handling, seller escalation, damaged items, and customer communication.

These documents are useful for future RAG workflows.

### Troubleshooting Guides

Troubleshooting guides describe product-category-specific support procedures.

They will later be embedded in Qdrant and used by the RAG Retrieval Agent.

## Design Principles

The synthetic generation process follows these principles:

* preserve real transactional IDs
* avoid disconnected random records
* generate deterministic output using a random seed
* create both structured fields and natural-language text
* include metadata for future Qdrant filtering
* support future Neo4j graph extension
* support future hybrid retrieval evaluation
* keep the generator free and open source
* avoid using paid LLM APIs at this stage

## Role in the Final Platform

The generated synthetic artifacts will later support:

* Graph RAG
* vector retrieval
* hybrid retrieval
* support intelligence
* complaint analysis
* executive reporting
* multi-agent workflow orchestration
* retrieval evaluation datasets

This stage turns the project from a transactional database system into an enterprise knowledge intelligence platform.
