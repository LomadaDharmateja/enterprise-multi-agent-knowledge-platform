// ============================================================
// Enterprise Knowledge Intelligence Platform
// Neo4j Indexes
// ============================================================

// Customer lookup and segmentation
CREATE INDEX customer_unique_id_index IF NOT EXISTS
FOR (c:Customer)
ON (c.customer_unique_id);

CREATE INDEX customer_state_index IF NOT EXISTS
FOR (c:Customer)
ON (c.customer_state);

// Order lifecycle and analytics
CREATE INDEX order_status_index IF NOT EXISTS
FOR (o:Order)
ON (o.order_status);

CREATE INDEX order_delivery_status_index IF NOT EXISTS
FOR (o:Order)
ON (o.delivery_status);

CREATE INDEX order_late_delivery_index IF NOT EXISTS
FOR (o:Order)
ON (o.is_late_delivery);

// Product and category investigation
CREATE INDEX product_category_index IF NOT EXISTS
FOR (p:Product)
ON (p.product_category_name);

CREATE INDEX category_english_name_index IF NOT EXISTS
FOR (c:Category)
ON (c.product_category_name_english);

// Seller analytics
CREATE INDEX seller_state_index IF NOT EXISTS
FOR (s:Seller)
ON (s.seller_state);

// Payment investigation
CREATE INDEX payment_type_index IF NOT EXISTS
FOR (p:Payment)
ON (p.payment_type);

// Review intelligence
CREATE INDEX review_score_index IF NOT EXISTS
FOR (r:Review)
ON (r.review_score);

CREATE INDEX review_sentiment_index IF NOT EXISTS
FOR (r:Review)
ON (r.sentiment_label);

// Region traversal
CREATE INDEX region_state_index IF NOT EXISTS
FOR (r:Region)
ON (r.state);

CREATE INDEX region_city_index IF NOT EXISTS
FOR (r:Region)
ON (r.city);