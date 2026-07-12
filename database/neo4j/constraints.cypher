// ============================================================
// Enterprise Knowledge Intelligence Platform
// Neo4j Constraints
// ============================================================

CREATE CONSTRAINT customer_id_unique IF NOT EXISTS
FOR (c:Customer)
REQUIRE c.customer_id IS UNIQUE;

CREATE CONSTRAINT order_id_unique IF NOT EXISTS
FOR (o:Order)
REQUIRE o.order_id IS UNIQUE;

CREATE CONSTRAINT order_item_key_unique IF NOT EXISTS
FOR (oi:OrderItem)
REQUIRE oi.order_item_key IS UNIQUE;

CREATE CONSTRAINT product_id_unique IF NOT EXISTS
FOR (p:Product)
REQUIRE p.product_id IS UNIQUE;

CREATE CONSTRAINT seller_id_unique IF NOT EXISTS
FOR (s:Seller)
REQUIRE s.seller_id IS UNIQUE;

CREATE CONSTRAINT payment_key_unique IF NOT EXISTS
FOR (p:Payment)
REQUIRE p.payment_key IS UNIQUE;

CREATE CONSTRAINT review_key_unique IF NOT EXISTS
FOR (r:Review)
REQUIRE r.review_key IS UNIQUE;

CREATE CONSTRAINT category_id_unique IF NOT EXISTS
FOR (c:Category)
REQUIRE c.category_id IS UNIQUE;

CREATE CONSTRAINT region_id_unique IF NOT EXISTS
FOR (r:Region)
REQUIRE r.region_id IS UNIQUE;