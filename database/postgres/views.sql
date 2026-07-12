-- ============================================================
-- Enterprise Knowledge Intelligence Platform
-- PostgreSQL Business Intelligence Views
-- ============================================================

SET search_path TO ecommerce;

-- ============================================================
-- View 1: Order Summary
-- One row per order with customer, item, payment, and review context.
-- ============================================================

CREATE OR REPLACE VIEW ecommerce.vw_order_summary AS
WITH order_item_agg AS (
    SELECT
        order_id,
        COUNT(*) AS item_count,
        COUNT(DISTINCT product_id) AS unique_product_count,
        COUNT(DISTINCT seller_id) AS unique_seller_count,
        SUM(price) AS total_item_value,
        SUM(freight_value) AS total_freight_value,
        SUM(price + freight_value) AS total_order_item_value
    FROM ecommerce.order_items
    GROUP BY order_id
),

payment_agg AS (
    SELECT
        order_id,
        COUNT(*) AS payment_record_count,
        COUNT(DISTINCT payment_type) AS payment_type_count,
        STRING_AGG(DISTINCT payment_type, ', ' ORDER BY payment_type) AS payment_types,
        SUM(payment_value) AS total_payment_value,
        MAX(payment_installments) AS max_payment_installments
    FROM ecommerce.payments
    GROUP BY order_id
),

review_agg AS (
    SELECT
        order_id,
        COUNT(*) AS review_count,
        ROUND(AVG(review_score)::NUMERIC, 2) AS avg_review_score,
        MIN(review_score) AS min_review_score,
        MAX(review_score) AS max_review_score,
        STRING_AGG(DISTINCT sentiment_label, ', ' ORDER BY sentiment_label) AS sentiment_labels
    FROM ecommerce.reviews
    GROUP BY order_id
)

SELECT
    o.order_id,
    o.customer_id,
    c.customer_unique_id,
    c.customer_city,
    c.customer_state,
    o.order_status,
    o.approval_status,
    o.shipment_status,
    o.delivery_status,
    o.order_purchase_timestamp,
    o.order_approved_at,
    o.order_delivered_carrier_date,
    o.order_delivered_customer_date,
    o.order_estimated_delivery_date,

    ROUND(
        (EXTRACT(EPOCH FROM (o.order_delivered_customer_date - o.order_purchase_timestamp)) / 86400)::NUMERIC,
        2
    ) AS purchase_to_delivery_days,

    ROUND(
        (EXTRACT(EPOCH FROM (o.order_delivered_customer_date - o.order_estimated_delivery_date)) / 86400)::NUMERIC,
        2
    ) AS delivery_delay_days,

    CASE
        WHEN o.order_delivered_customer_date IS NULL THEN NULL
        WHEN o.order_estimated_delivery_date IS NULL THEN NULL
        WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN TRUE
        ELSE FALSE
    END AS is_late_delivery,

    COALESCE(oi.item_count, 0) AS item_count,
    COALESCE(oi.unique_product_count, 0) AS unique_product_count,
    COALESCE(oi.unique_seller_count, 0) AS unique_seller_count,
    COALESCE(oi.total_item_value, 0) AS total_item_value,
    COALESCE(oi.total_freight_value, 0) AS total_freight_value,
    COALESCE(oi.total_order_item_value, 0) AS total_order_item_value,

    COALESCE(p.payment_record_count, 0) AS payment_record_count,
    COALESCE(p.payment_type_count, 0) AS payment_type_count,
    p.payment_types,
    COALESCE(p.total_payment_value, 0) AS total_payment_value,
    p.max_payment_installments,

    COALESCE(r.review_count, 0) AS review_count,
    r.avg_review_score,
    r.min_review_score,
    r.max_review_score,
    r.sentiment_labels

FROM ecommerce.orders o
LEFT JOIN ecommerce.customers c
    ON o.customer_id = c.customer_id
LEFT JOIN order_item_agg oi
    ON o.order_id = oi.order_id
LEFT JOIN payment_agg p
    ON o.order_id = p.order_id
LEFT JOIN review_agg r
    ON o.order_id = r.order_id;


-- ============================================================
-- View 2: Customer Order History
-- One row per unique customer profile.
-- ============================================================

CREATE OR REPLACE VIEW ecommerce.vw_customer_order_history AS
SELECT
    c.customer_unique_id,
    COUNT(DISTINCT c.customer_id) AS customer_record_count,
    COUNT(DISTINCT o.order_id) AS total_orders,
    MIN(o.order_purchase_timestamp) AS first_order_timestamp,
    MAX(o.order_purchase_timestamp) AS last_order_timestamp,
    STRING_AGG(DISTINCT c.customer_state, ', ' ORDER BY c.customer_state) AS customer_states,
    STRING_AGG(DISTINCT c.customer_city, ', ' ORDER BY c.customer_city) AS customer_cities,
    COALESCE(SUM(os.total_payment_value), 0) AS total_customer_payment_value,
    ROUND(AVG(os.total_payment_value)::NUMERIC, 2) AS avg_order_payment_value,
    ROUND(AVG(os.avg_review_score)::NUMERIC, 2) AS avg_customer_review_score,
    SUM(CASE WHEN os.is_late_delivery THEN 1 ELSE 0 END) AS late_delivery_orders
FROM ecommerce.customers c
LEFT JOIN ecommerce.orders o
    ON c.customer_id = o.customer_id
LEFT JOIN ecommerce.vw_order_summary os
    ON o.order_id = os.order_id
GROUP BY c.customer_unique_id;


-- ============================================================
-- View 3: Seller Performance
-- One row per seller with revenue, order, delivery, and review signals.
-- ============================================================

CREATE OR REPLACE VIEW ecommerce.vw_seller_performance AS
WITH seller_order_item_agg AS (
    SELECT
        oi.seller_id,
        oi.order_id,
        COUNT(*) AS seller_item_count,
        COUNT(DISTINCT oi.product_id) AS seller_unique_products_in_order,
        SUM(oi.price) AS seller_item_revenue,
        SUM(oi.freight_value) AS seller_freight_value
    FROM ecommerce.order_items oi
    GROUP BY oi.seller_id, oi.order_id
),

seller_review_agg AS (
    SELECT
        so.seller_id,
        ROUND(AVG(r.review_score)::NUMERIC, 2) AS avg_review_score,
        COUNT(r.review_id) AS review_count
    FROM seller_order_item_agg so
    LEFT JOIN ecommerce.reviews r
        ON so.order_id = r.order_id
    GROUP BY so.seller_id
)

SELECT
    s.seller_id,
    s.seller_city,
    s.seller_state,
    COUNT(DISTINCT so.order_id) AS total_orders,
    SUM(so.seller_item_count) AS total_items_sold,
    SUM(so.seller_item_revenue) AS total_item_revenue,
    SUM(so.seller_freight_value) AS total_freight_value,
    ROUND(AVG(so.seller_item_revenue)::NUMERIC, 2) AS avg_revenue_per_order,
    sr.avg_review_score,
    sr.review_count,
    SUM(CASE WHEN os.is_late_delivery THEN 1 ELSE 0 END) AS late_delivery_orders
FROM ecommerce.sellers s
LEFT JOIN seller_order_item_agg so
    ON s.seller_id = so.seller_id
LEFT JOIN ecommerce.vw_order_summary os
    ON so.order_id = os.order_id
LEFT JOIN seller_review_agg sr
    ON s.seller_id = sr.seller_id
GROUP BY
    s.seller_id,
    s.seller_city,
    s.seller_state,
    sr.avg_review_score,
    sr.review_count;


-- ============================================================
-- View 4: Product Performance
-- One row per product with sales and review context.
-- ============================================================

CREATE OR REPLACE VIEW ecommerce.vw_product_performance AS
WITH product_order_item_agg AS (
    SELECT
        oi.product_id,
        oi.order_id,
        COUNT(*) AS product_item_count,
        SUM(oi.price) AS product_revenue,
        SUM(oi.freight_value) AS product_freight_value
    FROM ecommerce.order_items oi
    GROUP BY oi.product_id, oi.order_id
),

product_review_agg AS (
    SELECT
        po.product_id,
        ROUND(AVG(r.review_score)::NUMERIC, 2) AS avg_review_score,
        COUNT(r.review_id) AS review_count
    FROM product_order_item_agg po
    LEFT JOIN ecommerce.reviews r
        ON po.order_id = r.order_id
    GROUP BY po.product_id
)

SELECT
    p.product_id,
    p.product_category_name,
    pct.product_category_name_english,
    p.product_weight_g,
    p.product_length_cm,
    p.product_height_cm,
    p.product_width_cm,
    COUNT(DISTINCT po.order_id) AS total_orders,
    SUM(po.product_item_count) AS total_items_sold,
    SUM(po.product_revenue) AS total_product_revenue,
    SUM(po.product_freight_value) AS total_product_freight_value,
    ROUND(AVG(po.product_revenue)::NUMERIC, 2) AS avg_revenue_per_order,
    pr.avg_review_score,
    pr.review_count
FROM ecommerce.products p
LEFT JOIN product_order_item_agg po
    ON p.product_id = po.product_id
LEFT JOIN ecommerce.product_category_translations pct
    ON p.product_category_name = pct.product_category_name
LEFT JOIN product_review_agg pr
    ON p.product_id = pr.product_id
GROUP BY
    p.product_id,
    p.product_category_name,
    pct.product_category_name_english,
    p.product_weight_g,
    p.product_length_cm,
    p.product_height_cm,
    p.product_width_cm,
    pr.avg_review_score,
    pr.review_count;


-- ============================================================
-- View 5: Review Intelligence
-- One row per review with order and customer context.
-- ============================================================

CREATE OR REPLACE VIEW ecommerce.vw_review_intelligence AS
SELECT
    r.review_id,
    r.order_id,
    o.customer_id,
    c.customer_unique_id,
    c.customer_city,
    c.customer_state,
    o.order_status,
    o.order_purchase_timestamp,
    o.order_delivered_customer_date,
    r.review_score,
    r.sentiment_label,
    r.review_comment_title,
    r.review_comment_message,
    r.review_creation_date,
    r.review_answer_timestamp,

    CASE
        WHEN r.review_score <= 2 THEN TRUE
        ELSE FALSE
    END AS is_negative_review,

    CASE
        WHEN r.review_comment_message IS NULL THEN FALSE
        WHEN LENGTH(TRIM(r.review_comment_message)) = 0 THEN FALSE
        ELSE TRUE
    END AS has_review_comment

FROM ecommerce.reviews r
LEFT JOIN ecommerce.orders o
    ON r.order_id = o.order_id
LEFT JOIN ecommerce.customers c
    ON o.customer_id = c.customer_id;


-- ============================================================
-- View 6: Payment Summary
-- One row per order with payment behavior.
-- ============================================================

CREATE OR REPLACE VIEW ecommerce.vw_payment_summary AS
SELECT
    p.order_id,
    o.customer_id,
    c.customer_unique_id,
    o.order_status,
    COUNT(*) AS payment_record_count,
    COUNT(DISTINCT p.payment_type) AS payment_type_count,
    STRING_AGG(DISTINCT p.payment_type, ', ' ORDER BY p.payment_type) AS payment_types,
    SUM(p.payment_value) AS total_payment_value,
    MAX(p.payment_installments) AS max_payment_installments,
    ROUND(AVG(p.payment_installments)::NUMERIC, 2) AS avg_payment_installments
FROM ecommerce.payments p
LEFT JOIN ecommerce.orders o
    ON p.order_id = o.order_id
LEFT JOIN ecommerce.customers c
    ON o.customer_id = c.customer_id
GROUP BY
    p.order_id,
    o.customer_id,
    c.customer_unique_id,
    o.order_status;