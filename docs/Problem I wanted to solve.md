I developed a multi-agent enterprise knowledge-intelligence platform designed to answer complex business questions by combining structured data, relationship data and unstructured enterprise knowledge.

### Problem I wanted to solve

The main engineering problem was enterprise information fragmentation.

In a real organisation, all the information required to answer a business question is rarely available in one database.

For example, consider the question:

“Which sellers are associated with negative customer complaints, product warranty issues and relevant support policies?”

To answer this properly, the system may need:

- Structured seller, order and review statistics.
- Relationships between customers, orders, products, reviews and sellers.
- Semantically relevant support tickets and warranty claims.
- Applicable policy or troubleshooting documents.

A normal chatbot cannot reliably answer such a question because it does not have direct access to the company’s operational data. A vector-only RAG system would also be insufficient because vector retrieval is suitable for documents, but it is not ideal for exact calculations, rankings or multi-hop entity relationships.

Therefore, I designed a hybrid agentic architecture combining PostgreSQL, Neo4j and Qdrant.

### Data foundation

I used the Olist Brazilian e-commerce dataset as the core structured dataset.

The dataset contains information about:

- Customers.
- Sellers.
- Products.
- Product categories.
- Orders.
- Order items.
- Payments.
- Customer reviews.

I loaded and validated approximately:

- 99,441 customers.
- 3,095 sellers.
- 32,340 products.
- 99,441 orders.
- 111,046 order items.
- 103,886 payments.
- 99,224 reviews.
- 73 product-category translations.

### PostgreSQL layer

I used PostgreSQL for structured transactional analytics.

PostgreSQL is suitable when a user asks questions that require:

- Exact counts.
- Aggregations.
- Revenue calculations.
- Rankings.
- Filtering.
- Joins.
- Delivery-performance calculations.
- Payment analysis.
- Seller or product statistics.

I created the database schema, added indexes and loaded all Olist CSV files into their corresponding tables.

I also created and validated six business views. These views supported reusable enterprise analyses such as seller performance, delivery behaviour, payments, reviews, customers and product-level information.

I validated the database by checking the expected row counts and testing the business views.

### Neo4j knowledge graph

The second data layer was Neo4j.

I used Neo4j because many business questions are relationship-oriented. For example, a user may want to understand how a negative review is connected to a product, a seller, an order and a customer.

The graph contained nine node types:

- Customer.
- Seller.
- Category.
- Product.
- Order.
- OrderItem.
- Payment.
- Review.
- Region.

The graph contained eight main relationship types:

- Customer `PLACED` Order.
- Order `CONTAINS_ITEM` OrderItem.
- OrderItem `REFERENCES_PRODUCT` Product.
- OrderItem `SOLD_BY` Seller.
- Order `HAS_PAYMENT` Payment.
- Order `HAS_REVIEW` Review.
- Product `BELONGS_TO_CATEGORY` Category.
- Customer or seller `LOCATED_IN` Region.

I created graph constraints and indexes and then loaded the entities and relationships.

I also created a Neo4j validation process that checked:

- Node counts.
- Relationship counts.
- Missing relationships.
- Orphan nodes.
- Known business paths.

For example, it validated paths such as:

- Customer → Order → Payment.
- Customer → Order → Review.
- Negative Review → Order → Order Item → Product → Seller.

All required orphan checks passed with zero missing mandatory relationships.

### Synthetic enterprise knowledge

The Olist dataset mainly contains structured transactional records. To create a more realistic enterprise knowledge-retrieval use case, I generated additional synthetic artifacts.

I created:

- 3,000 support tickets.
- 3,000 customer emails.
- 1,000 logistics incidents.
- 1,000 warranty claims.
- 79 policy documents.
- 73 troubleshooting guides.

This produced a total of 8,152 synthetic enterprise records.

These records were connected to Olist entities using identifiers such as:

- Customer ID.
- Order ID.
- Product ID.
- Seller ID.

This was important because it allowed vector-search results to be connected back to PostgreSQL and Neo4j evidence.

For example, a retrieved warranty claim could reference a product ID and seller ID that also existed in PostgreSQL and Neo4j.

### Embeddings and Qdrant

I used Qdrant as the vector database.

I converted the text records into embeddings using the `all-MiniLM-L6-v2` sentence-transformer model.

I selected this model because it is:

- Open source.
- Lightweight.
- Suitable for semantic search.
- Fast enough to run locally.
- Practical for my available hardware.
- Free from external embedding API costs.

The model produces 384-dimensional vectors.

I created a Qdrant collection called `enterprise_knowledge` and indexed all 8,152 enterprise artifacts.

Each Qdrant record contained:

- The embedding vector.
- The original text.
- The artifact type.
- The record identifier.
- Relevant entity IDs.
- Source-specific metadata.

The planner could restrict retrieval to specific artifact groups such as support tickets, warranty claims, policies, troubleshooting guides, emails or logistics incidents.

Because the records were already short individual enterprise artifacts, I did not need a traditional long-document chunking strategy with fixed chunk sizes and overlap.

### Hybrid retrieval

After preparing the three data layers, I developed a hybrid retrieval system.

The purpose of hybrid retrieval was to use the most suitable database for each part of a user question.

The general selection logic was:

- PostgreSQL for exact structured facts and calculations.
- Neo4j for entity relationships and multi-hop paths.
- Qdrant for semantic retrieval from unstructured text.

For complex questions, the system could use two or all three sources.

I implemented deterministic retrieval tools instead of allowing the LLM to execute unrestricted database commands.

This means the LLM could select from approved business routes, but it could not generate and execute arbitrary SQL, Cypher, Python or shell commands.

This approach improved safety and predictability.

### Why I used an agentic architecture

A fixed routing system can work when every question follows known keywords. However, enterprise users may express the same requirement in many different ways.

For example:

- “Which sellers have the most complaints?”
- “Find vendors connected to poor customer experiences.”
- “Show suppliers associated with negative reviews and warranty issues.”

These questions may have similar business intent even though their wording is different.

Therefore, I used an LLM planner to interpret the meaning of the question and produce a structured retrieval plan.

The architecture is agentic because:

- An LLM interprets the user’s objective.
- It dynamically selects tools and data sources.
- Tool results are used by downstream reasoning components.
- Multiple specialised agents perform different responsibilities.
- LangGraph maintains shared state across the workflow.
- The final answer is evaluated by another agent.

It is a controlled agentic system, not an unrestricted autonomous agent.

### LangGraph orchestration

I used LangGraph as the orchestration framework.

LangGraph manages the workflow as a graph of connected steps and maintains a shared workflow state.

The workflow can be represented as:

User question  
→ Planner  
→ Hybrid retrieval  
→ Context construction  
→ Answer generation  
→ Answer evaluation  
→ Final response

The shared state contains information such as:

- Original query.
- Planner output.
- Selected routes.
- SQL results.
- Graph results.
- Vector results.
- Constructed context.
- Generated answer.
- Evaluation result.
- Workflow status.

Each component reads the fields it requires and writes its output back into the shared state.

I selected LangGraph because it provides a clear and inspectable workflow. It is easier to debug than putting all responsibilities into one large prompt or one general-purpose agent.

### Planner agent

The first LLM component is the Gemini planner agent.

The planner receives:

- The natural-language user question.
- Instructions about available data sources.
- Allowed SQL routes.
- Allowed graph routes.
- Allowed vector artifact groups.
- The expected structured-output format.

The planner produces a structured plan showing whether the question requires:

- SQL retrieval.
- Graph retrieval.
- Vector retrieval.
- Or a combination.

For example, if the question asks for the highest-revenue seller, the planner should select PostgreSQL.

If the question asks how a customer complaint is connected to a product and seller, it may select Neo4j.

If the question asks for relevant warranty policies or similar support incidents, it may select Qdrant.

If the question contains all these requirements, it can select all three.

The planner output was not executed directly as unrestricted code. It was validated and mapped to predefined deterministic retrieval functions.

### Deterministic retrieval tools

After planning, the selected retrieval tools were executed.

The PostgreSQL tool returned structured business facts and calculations.

The Neo4j tool returned graph nodes, relationships and connected paths.

The Qdrant tool returned semantically ranked enterprise records and metadata.

The tools returned predictable dictionaries and lists rather than free-form text.

This made it easier to combine the results and pass them through the workflow.

### Retrieval context builder

The results from PostgreSQL, Neo4j and Qdrant have different structures.

SQL may return tables and aggregated values.

Neo4j may return nodes and relationship paths.

Qdrant may return text documents with similarity scores and metadata.

Therefore, I implemented a retrieval context builder.

Its responsibility was to:

- Normalise the results.
- Keep the source types clearly separated.
- Group related evidence using entity IDs.
- Remove unnecessary fields.
- Construct a readable and controlled evidence context.
- Prepare the input for the answer-generation agent.

The context-builder validator passed all five representative validation cases.

### Answer-generation agent

The second LLM component is the Gemini answer agent.

It receives:

- The original business question.
- The structured retrieval context.
- Instructions to use only the provided evidence.
- Instructions not to invent unsupported information.
- Instructions to identify missing evidence when required.

Its job is to combine the evidence into a business-readable explanation.

For example, it may combine:

- A PostgreSQL seller-performance result.
- A Neo4j customer–order–product–seller path.
- Several Qdrant warranty claims and support policies.

The answer agent does not independently access unrestricted tools. It works with the context created by the retrieval pipeline.

### Evaluation agent

The third LLM component is the Gemini evaluation agent.

Its responsibility is not to answer the original question. Its responsibility is to check the quality of the generated answer.

The evaluator checks areas such as:

- Whether the answer is grounded in the retrieved evidence.
- Whether important parts of the question were answered.
- Whether unsupported claims were added.
- Whether relevant evidence is missing.
- Whether the response is useful from a business perspective.

The output includes information such as:

- Grounding score.
- Completeness score.
- Business-readiness score.
- Unsupported claims.
- Missing evidence.
- Deterministic validation failures.
- Overall evaluation status.

I also used deterministic checks alongside the LLM evaluator. This is important because the evaluator itself is also an LLM and should not be trusted blindly.

One limitation is that the workflow currently stops after evaluation. If the evaluator rejects an answer, it reports the problem, but the system does not automatically return to retrieval or regenerate the answer. This would be a valuable future improvement.

### FastAPI backend

After completing the LangGraph workflow, I exposed it through FastAPI.

I implemented two main endpoints:

- `GET /health`
- `POST /query`

The `/health` endpoint checks whether the backend and workflow are available.

The `/query` endpoint accepts a natural-language question, executes the complete workflow and returns a structured JSON response.

The response contains information such as:

- Workflow status.
- Generated answer.
- SQL route summary.
- Graph route summary.
- Vector route summary.
- Evaluation results.
- Output-report locations.

The API validator passed two endpoint checks and five query cases.

### Streamlit interface

I developed a Streamlit interface so that the project could be used without manually calling the API.

The interface allows users to:

- Check the API health status.
- Enter a natural-language enterprise question.
- Select example questions.
- Run the complete workflow.
- View the generated answer.
- See which SQL, graph and vector routes were selected.
- View selected vector artifact groups.
- View evaluation scores.
- Inspect unsupported claims and missing evidence.
- View workflow and report information.

The Streamlit interface communicates with the FastAPI backend rather than directly calling all the databases.

This keeps the UI separated from the application logic.

### Observability

I added local observability to make the workflow inspectable.

The system records events and metrics under the `reports/observability` directory.

It generates:

- JSONL event logs.
- CSV metrics.
- An observability report.

A successful workflow run produced eight events across four traced components:

- Planner.
- Hybrid retrieval.
- Answer generation.
- Evaluation.

For each traced component, the logs can record:

- Operation name.
- Start event.
- Completion event.
- Execution status.
- Duration.
- Error information when available.
- Workflow-run metadata.

This helped me confirm that each stage executed in the expected order and identify which stage failed during debugging.

Token counts were not added to the observability system, which is another future improvement.

### Docker deployment

I containerised the complete project using Docker Compose.

Five main services run separately:

1. PostgreSQL.
2. Neo4j.
3. Qdrant.
4. FastAPI.
5. Streamlit.

The services communicate through the Docker network using service names and internal ports.

For example:

- FastAPI communicates with PostgreSQL, Neo4j and Qdrant.
- Streamlit communicates with FastAPI.
- The databases do not need to communicate directly with Streamlit.

I performed a complete local Docker runtime test.

The following passed:

- PostgreSQL container.
- Neo4j container.
- Qdrant container.
- FastAPI container.
- Streamlit container.
- API health validation.
- API query validation.
- User-interface access.
- Observability output.

This means the project was not only developed as individual scripts. The complete application was run and validated as an integrated local deployment.

### System evaluation

I created five representative end-to-end validation questions.

The questions were selected to cover different retrieval combinations, including:

- SQL-only or SQL-heavy questions.
- Graph relationship questions.
- Vector-document retrieval.
- Hybrid SQL, graph and vector questions.
- Seller, delivery, complaint, product, payment and warranty scenarios.

The final agentic workflow passed all five cases.

However, I would not describe this as 100% general accuracy. It means the workflow achieved five out of five successful results on a small predefined functional validation set.

I did not create a large benchmark with hundreds of human-labelled questions.

I also did not formally measure:

- Retrieval precision.
- Retrieval recall.
- Hallucination rate.
- Planner accuracy over a large dataset.
- Average latency.
- p95 latency.
- Token usage.
- Cost per query.
- Concurrent-user performance.

### Challenges and debugging

The biggest technical challenge was reliable multi-source routing.

Connecting PostgreSQL, Neo4j and Qdrant individually was manageable. The difficult part was deciding which combination was required for each natural-language question.

During early testing, some questions selected incomplete or unsuitable routes.

I corrected this by:

- Refining the routing logic.
- Improving planner instructions.
- Restricting the planner to approved route options.
- Adding deterministic support around the planner.
- Repeatedly running the same validation cases.
- Checking the selected routes and retrieval results.

After these refinements, all five workflow-validation cases passed.

### Security approach

The project included some basic security-oriented design decisions:

- The LLM cannot execute arbitrary shell commands.
- The LLM cannot run unrestricted Python.
- It does not generate and execute arbitrary SQL or Cypher.
- Tool actions are allowlisted.
- The runtime workflow is intended to be read-only.
- Credentials are provided through environment configuration.
- The project uses structured API requests.

However, the system is not yet enterprise-security ready.

It does not currently include:

- User authentication.
- Role-based authorisation.
- Row-level security.
- Document-level permissions.
- Managed secret storage.
- Prompt-injection protection.
- Security-grade audit logs.
- Formal GDPR or data-protection evaluation.

Because Gemini is an external provider, the user query and selected retrieval evidence may be sent to the Gemini service. This would need to be carefully evaluated before using confidential company information.

### Current limitations

The main limitations are:

- Only five end-to-end validation questions.
- Synthetic support and operational documents.
- No human-calibrated evaluation dataset.
- No single-agent baseline comparison.
- No automatic replanning.
- No answer-regeneration loop.
- No retrieval reranker.
- No caching.
- No automatic retries or circuit breakers.
- No load testing.
- No token or financial-cost calculation.
- No authentication or authorisation.
- No cloud deployment.
- No CI/CD.
- No production backup and disaster recovery.
- No high-availability configuration.

Therefore, I describe it as a production-oriented, fully validated local prototype rather than a completely production-ready application.

### What I would improve next

My first technical improvement would be evaluator-triggered recovery.

If the evaluator detects poor grounding or missing evidence, the workflow could:

1. Return to the planner.
2. Revise the route.
3. Retrieve additional information.
4. Regenerate the answer.
5. Evaluate it again.

I would strictly limit this to one additional attempt to avoid uncontrolled agent loops.

My second improvement would be a larger evaluation framework.

I would create at least 50 to 100 labelled questions and record:

- Expected routes.
- Expected tools.
- Expected source records.
- Reference answers.
- Retrieval relevance labels.

This would allow me to measure planner accuracy, precision, recall, hit rate, grounding, answer correctness and hallucination rate.

My third improvement would be production security and deployment.

I would add:

- Authentication.
- Role-based access.
- Read-only database accounts.
- Managed secrets.
- Prompt-injection controls.
- CI/CD.
- Cloud deployment.
- Monitoring.
- Backups.
- Load testing.
- Rate limiting.

### Final explanation of my contribution

My personal contribution was designing and implementing the project step by step.

I prepared the databases, loaded and validated the data, created the graph, generated synthetic enterprise artifacts, built the vector index, implemented hybrid retrieval, integrated Gemini agents, created the LangGraph workflow, exposed it through FastAPI, developed the Streamlit interface, added observability, containerised the complete platform and ran the final validators.

I used AI-assisted development for architecture guidance, code generation and debugging, but I executed the commands, analysed the errors, made the design decisions, integrated the components and validated the final system.

In one sentence, I would describe the project as:

“I developed a multi-agent enterprise AI platform that uses LangGraph and Gemini agents to dynamically combine PostgreSQL, Neo4j and Qdrant evidence and produce evaluated, grounded answers to business questions.”