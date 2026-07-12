-- ============================================================
-- Enterprise Knowledge Intelligence Platform
-- PostgreSQL Relational Schema
-- Dataset Backbone: Olist Brazilian E-Commerce Dataset
-- ============================================================

CREATE SCHEMA IF NOT EXISTS ecommerce;

SET search_path TO ecommerce;

-- ============================================================
-- Drop tables for local development reset
-- ============================================================

DROP TABLE IF EXISTS reviews CASCADE;
DROP TABLE IF EXISTS payments CASCADE;
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS sellers CASCADE;
DROP TABLE IF EXISTS customers CASCADE;
DROP TABLE IF EXISTS product_category_translations CASCADE;

-- ============================================================
-- Customers
-- ============================================================

CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    customer_unique_id TEXT NOT NULL,
    customer_zip_code_prefix INTEGER,
    customer_city TEXT,
    customer_state TEXT
);

-- ============================================================
-- Sellers
-- ============================================================

CREATE TABLE sellers (
    seller_id TEXT PRIMARY KEY,
    seller_zip_code_prefix INTEGER,
    seller_city TEXT,
    seller_state TEXT
);

-- ============================================================
-- Product Category Translations
-- Optional reference table.
-- This table connects Portuguese product category names
-- to English category names.
-- ============================================================

CREATE TABLE product_category_translations (
    product_category_name TEXT PRIMARY KEY,
    product_category_name_english TEXT
);

-- ============================================================
-- Products
-- ============================================================

CREATE TABLE products (
    product_id TEXT PRIMARY KEY,
    product_category_name TEXT,
    product_name_lenght INTEGER,
    product_description_lenght INTEGER,
    product_photos_qty INTEGER,
    product_weight_g INTEGER,
    product_length_cm INTEGER,
    product_height_cm INTEGER,
    product_width_cm INTEGER,

    CONSTRAINT fk_products_category
        FOREIGN KEY (product_category_name)
        REFERENCES product_category_translations(product_category_name)
        ON UPDATE CASCADE
        ON DELETE SET NULL
);

-- ============================================================
-- Orders
-- ============================================================

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    order_status TEXT,
    order_purchase_timestamp TIMESTAMP,
    order_approved_at TIMESTAMP,
    order_delivered_carrier_date TIMESTAMP,
    order_delivered_customer_date TIMESTAMP,
    order_estimated_delivery_date TIMESTAMP,

    -- Workflow state columns created during cleaning.
    -- Keep these nullable because exact values depend on the cleaning logic.
    approval_status TEXT,
    shipment_status TEXT,
    delivery_status TEXT,

    CONSTRAINT fk_orders_customer
        FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

-- ============================================================
-- Order Items
-- Composite primary key because one order can contain
-- multiple item rows.
-- ============================================================

CREATE TABLE order_items (
    order_id TEXT NOT NULL,
    order_item_id INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    seller_id TEXT NOT NULL,
    shipping_limit_date TIMESTAMP,
    price NUMERIC(12, 2),
    freight_value NUMERIC(12, 2),

    PRIMARY KEY (order_id, order_item_id),

    CONSTRAINT fk_order_items_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,

    CONSTRAINT fk_order_items_product
        FOREIGN KEY (product_id)
        REFERENCES products(product_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_order_items_seller
        FOREIGN KEY (seller_id)
        REFERENCES sellers(seller_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

-- ============================================================
-- Payments
-- Composite primary key because an order may have multiple
-- payment records.
-- ============================================================

CREATE TABLE payments (
    order_id TEXT NOT NULL,
    payment_sequential INTEGER NOT NULL,
    payment_type TEXT,
    payment_installments INTEGER,
    payment_value NUMERIC(12, 2),

    PRIMARY KEY (order_id, payment_sequential),

    CONSTRAINT fk_payments_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

-- ============================================================
-- Reviews
-- Composite primary key because review_id alone is not unique
-- in the validated cleaned dataset.
-- ============================================================

CREATE TABLE reviews (
    review_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    review_score INTEGER,
    review_comment_title TEXT,
    review_comment_message TEXT,
    review_creation_date TIMESTAMP,
    review_answer_timestamp TIMESTAMP,
    sentiment_label TEXT,

    PRIMARY KEY (review_id, order_id),

    CONSTRAINT fk_reviews_order
        FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,

    CONSTRAINT chk_review_score
        CHECK (
            review_score IS NULL
            OR review_score BETWEEN 1 AND 5
        )
);