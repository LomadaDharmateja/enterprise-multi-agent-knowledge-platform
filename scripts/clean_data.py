"""Stage 1 of the pipeline: clean the raw Olist CSVs into data/processed/.

Provenance
----------
Before M0 this stage existed only as notebooks/01_olist_exploration.ipynb, which is
covered by the `notebooks/` rule in .gitignore. The first stage of the pipeline was
therefore not in the repository at all, and a clean clone could not produce the
`*_cleaned.csv` files that src/database/postgres_loader.py reads (AUDIT.md P4).

This script is a faithful transcription of that notebook's cleaning cells -- cells
04, 05, 10, 12, 15, 21, 23, 24, 25, 26 and 34. It is deliberately NOT an improvement
on it. Two known defects are reproduced exactly, because M0 freezes behaviour rather
than fixing it:

  * The reviews sentiment column is named `sentiment`. postgres_loader.py:129 expects
    `sentiment_label`, and standardize_columns() (postgres_loader.py:233-235) silently
    invents the missing column as NULL, which is why sentiment_label is NULL for all
    99,224 rows in ecommerce.reviews (AUDIT.md F-05). Renaming it here would change
    the database, which is an M1 change, not an M0 one.
  * translations_cleaned.csv is a straight copy of the raw translation file. It covers
    71 of the 73 category names that appear in products, so two categories end up with
    no English name (AUDIT.md F-06).

Usage
-----
    python scripts/clean_data.py
    python scripts/clean_data.py --raw-dir data/raw --output-dir data/processed
    python scripts/clean_data.py --output-dir /tmp/check --report /tmp/check/report.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_FILES = {
    "customers": "olist_customers_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "products": "olist_products_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "translations": "product_category_name_translation.csv",
}

OUTPUT_FILES = {
    "customers": "customers_cleaned.csv",
    "orders": "orders_cleaned.csv",
    "order_items": "order_items_cleaned.csv",
    "products": "products_cleaned.csv",
    "payments": "payments_cleaned.csv",
    "reviews": "reviews_cleaned.csv",
    "sellers": "sellers_cleaned.csv",
    "geolocation": "geolocation_cleaned.csv",
    "translations": "translations_cleaned.csv",
}

PRODUCT_REQUIRED_COLUMNS = [
    "product_category_name",
    "product_name_lenght",  # sic -- the misspelling is in the source dataset
    "product_description_lenght",
    "product_photos_qty",
    "product_weight_g",
    "product_length_cm",
    "product_height_cm",
    "product_width_cm",
]


def load_raw(raw_dir: Path) -> dict[str, pd.DataFrame]:
    missing = [name for name in RAW_FILES.values() if not (raw_dir / name).exists()]
    if missing:
        raise SystemExit(
            f"Missing raw Olist files in {raw_dir}:\n  "
            + "\n  ".join(missing)
            + "\n\nDownload the Brazilian E-Commerce Public Dataset by Olist from\n"
            "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce and unzip it\n"
            f"into {raw_dir}. See docs/SETUP.md."
        )
    return {key: pd.read_csv(raw_dir / name) for key, name in RAW_FILES.items()}


def clean_customers(customers: pd.DataFrame) -> pd.DataFrame:
    """Notebook cells 04-05: normalise city casing and state casing."""
    customers = customers.copy()
    customers["customer_city"] = customers["customer_city"].str.lower().str.strip()
    customers["customer_state"] = customers["customer_state"].str.upper().str.strip()
    return customers


def clean_products(products: pd.DataFrame) -> pd.DataFrame:
    """Notebook cells 10-12: drop products with any missing attribute."""
    return products.dropna(subset=PRODUCT_REQUIRED_COLUMNS)


def clean_order_items(
    order_items: pd.DataFrame, products_cleaned: pd.DataFrame
) -> pd.DataFrame:
    """Notebook cell 15: drop order items referencing a dropped product."""
    valid_product_ids = set(products_cleaned["product_id"])
    return order_items[order_items["product_id"].isin(valid_product_ids)]


def clean_orders(orders: pd.DataFrame) -> pd.DataFrame:
    """Notebook cell 21: derive status flags and the delivery delay in days."""
    orders = orders.copy()
    orders["approval_status"] = np.where(
        orders["order_approved_at"].isnull(), "not_approved", "approved"
    )
    orders["delivery_status"] = np.where(
        orders["order_delivered_customer_date"].isnull(), "not_delivered", "delivered"
    )
    orders["shipping_status"] = np.where(
        orders["order_delivered_carrier_date"].isnull(), "not_shipped", "shipped"
    )
    orders["order_purchase_timestamp"] = pd.to_datetime(orders["order_purchase_timestamp"])
    orders["order_delivered_customer_date"] = pd.to_datetime(
        orders["order_delivered_customer_date"]
    )
    orders["order_estimated_delivery_date"] = pd.to_datetime(
        orders["order_estimated_delivery_date"]
    )
    orders["delivery_delay_days"] = (
        orders["order_delivered_customer_date"] - orders["order_estimated_delivery_date"]
    ).dt.days
    return orders


def sentiment(score: int) -> str:
    """Notebook cell 24."""
    if score >= 4:
        return "positive"
    if score == 3:
        return "neutral"
    return "negative"


def clean_reviews(reviews: pd.DataFrame) -> pd.DataFrame:
    """Notebook cells 23-24.

    NOTE: the column is `sentiment`, not `sentiment_label`. See the module docstring
    -- this mismatch is reproduced on purpose.
    """
    reviews = reviews.copy()
    reviews["review_comment_message"] = reviews["review_comment_message"].fillna("no_comment")
    reviews["review_comment_title"] = reviews["review_comment_title"].fillna("no_title")
    reviews["sentiment"] = reviews["review_score"].apply(sentiment)
    return reviews


def clean_geolocation(geolocation: pd.DataFrame) -> pd.DataFrame:
    """Notebook cells 25-26: lowercase city, then drop exact duplicate rows."""
    geolocation = geolocation.copy()
    geolocation["geolocation_city"] = geolocation["geolocation_city"].str.lower().str.strip()
    return geolocation.drop_duplicates()


def check_referential_integrity(frames: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Notebook cells 17, 28, 30, 32. Reported, not enforced -- as in the notebook."""
    return {
        "order_items_without_product": len(
            set(frames["order_items"]["product_id"]) - set(frames["products"]["product_id"])
        ),
        "orders_without_customer": len(
            set(frames["orders"]["customer_id"]) - set(frames["customers"]["customer_id"])
        ),
        "order_items_without_seller": len(
            set(frames["order_items"]["seller_id"]) - set(frames["sellers"]["seller_id"])
        ),
    }


def clean_all(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    products_cleaned = clean_products(raw["products"])
    return {
        "customers": clean_customers(raw["customers"]),
        "orders": clean_orders(raw["orders"]),
        "order_items": clean_order_items(raw["order_items"], products_cleaned),
        "products": products_cleaned,
        "payments": raw["payments"],  # notebook cell 34: saved unmodified
        "reviews": clean_reviews(raw["reviews"]),
        "sellers": raw["sellers"],  # saved unmodified
        "geolocation": clean_geolocation(raw["geolocation"]),
        "translations": raw["translations"],  # saved unmodified
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean the raw Olist CSVs.")
    parser.add_argument("--raw-dir", default=str(PROJECT_ROOT / "data" / "raw"))
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "data" / "processed"))
    parser.add_argument("--report", default=None, help="optional path for a JSON summary")
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = load_raw(raw_dir)
    cleaned = clean_all(raw)
    integrity = check_referential_integrity(raw)

    tables: dict[str, object] = {}
    summary: dict[str, object] = {
        "raw_dir": str(raw_dir),
        "output_dir": str(output_dir),
        "referential_integrity": integrity,
        "tables": tables,
    }

    for key, frame in cleaned.items():
        path = output_dir / OUTPUT_FILES[key]
        frame.to_csv(path, index=False)
        tables[key] = {
            "file": OUTPUT_FILES[key],
            "raw_rows": int(len(raw[key])),
            "cleaned_rows": int(len(frame)),
            "dropped_rows": int(len(raw[key]) - len(frame)),
            "columns": list(frame.columns),
        }
        print(
            f"{OUTPUT_FILES[key]:<28} {len(raw[key]):>7} -> {len(frame):>7} rows "
            f"({len(raw[key]) - len(frame):>6} dropped)"
        )

    print("\nreferential integrity (reported, not enforced -- as in the notebook):")
    for name, count in integrity.items():
        print(f"  {name:<32} {count}")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nreport written to {report_path}")


if __name__ == "__main__":
    main()
