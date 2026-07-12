-- ============================================================
-- Enterprise Knowledge Intelligence Platform
-- PostgreSQL Indexes
-- ============================================================

SET search_path TO ecommerce;

-- ============================================================
-- Customer and location access patterns
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_customers_unique_id
ON customers(customer_unique_id);

CREATE INDEX IF NOT EXISTS idx_customers_state_city
ON customers(customer_state, customer_city);

CREATE INDEX IF NOT EXISTS idx_customers_zip
ON customers(customer_zip_code_prefix);

-- ============================================================
-- Seller location and vendor analytics
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_sellers_state_city
ON sellers(seller_state, seller_city);

CREATE INDEX IF NOT EXISTS idx_sellers_zip
ON sellers(seller_zip_code_prefix);

-- ============================================================
-- Product analytics
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_products_category
ON products(product_category_name);

-- ============================================================
-- Order lifecycle queries
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_orders_customer_id
ON orders(customer_id);

CREATE INDEX IF NOT EXISTS idx_orders_status
ON orders(order_status);

CREATE INDEX IF NOT EXISTS idx_orders_purchase_timestamp
ON orders(order_purchase_timestamp);

CREATE INDEX IF NOT EXISTS idx_orders_estimated_delivery
ON orders(order_estimated_delivery_date);

CREATE INDEX IF NOT EXISTS idx_orders_delivery_status
ON orders(delivery_status);

-- ============================================================
-- Order item joins and seller/product analytics
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_order_items_order_id
ON order_items(order_id);

CREATE INDEX IF NOT EXISTS idx_order_items_product_id
ON order_items(product_id);

CREATE INDEX IF NOT EXISTS idx_order_items_seller_id
ON order_items(seller_id);

CREATE INDEX IF NOT EXISTS idx_order_items_shipping_limit_date
ON order_items(shipping_limit_date);

-- ============================================================
-- Payment analytics
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_payments_order_id
ON payments(order_id);

CREATE INDEX IF NOT EXISTS idx_payments_type
ON payments(payment_type);

CREATE INDEX IF NOT EXISTS idx_payments_installments
ON payments(payment_installments);

-- ============================================================
-- Review and sentiment analytics
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_reviews_order_id
ON reviews(order_id);

CREATE INDEX IF NOT EXISTS idx_reviews_score
ON reviews(review_score);

CREATE INDEX IF NOT EXISTS idx_reviews_sentiment
ON reviews(sentiment_label);

CREATE INDEX IF NOT EXISTS idx_reviews_creation_date
ON reviews(review_creation_date);