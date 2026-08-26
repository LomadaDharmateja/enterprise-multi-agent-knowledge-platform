from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


FILE_CANDIDATES = {
    "customers": [
        "customers_cleaned.csv",
        "customers.csv",
        "olist_customers_dataset.csv",
    ],
    "sellers": [
        "sellers_cleaned.csv",
        "sellers.csv",
        "olist_sellers_dataset.csv",
    ],
    "products": [
        "products_cleaned.csv",
        "products.csv",
        "olist_products_dataset.csv",
    ],
    "orders": [
        "orders_cleaned.csv",
        "orders.csv",
        "olist_orders_dataset.csv",
    ],
    "order_items": [
        "order_items_cleaned.csv",
        "order_items.csv",
        "olist_order_items_dataset.csv",
    ],
    "payments": [
        "payments_cleaned.csv",
        "payments.csv",
        "order_payments_cleaned.csv",
        "olist_order_payments_dataset.csv",
    ],
    "reviews": [
        "reviews_cleaned.csv",
        "reviews.csv",
        "order_reviews_cleaned.csv",
        "olist_order_reviews_dataset.csv",
    ],
    "product_category_translations": [
        "translations_cleaned.csv",
        "product_category_translations_cleaned.csv",
        "product_category_name_translation.csv",
        "product_category_name_translation_cleaned.csv",
        "olist_product_category_name_translation_dataset.csv",
    ],
}


TABLE_COLUMNS = {
    "customers": [
        "customer_id",
        "customer_unique_id",
        "customer_zip_code_prefix",
        "customer_city",
        "customer_state",
    ],
    "sellers": [
        "seller_id",
        "seller_zip_code_prefix",
        "seller_city",
        "seller_state",
    ],
    "product_category_translations": [
        "product_category_name",
        "product_category_name_english",
    ],
    "products": [
        "product_id",
        "product_category_name",
        "product_name_lenght",
        "product_description_lenght",
        "product_photos_qty",
        "product_weight_g",
        "product_length_cm",
        "product_height_cm",
        "product_width_cm",
    ],
    "orders": [
        "order_id",
        "customer_id",
        "order_status",
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
        "approval_status",
        "shipment_status",
        "delivery_status",
    ],
    "order_items": [
        "order_id",
        "order_item_id",
        "product_id",
        "seller_id",
        "shipping_limit_date",
        "price",
        "freight_value",
    ],
    "payments": [
        "order_id",
        "payment_sequential",
        "payment_type",
        "payment_installments",
        "payment_value",
    ],
    "reviews": [
        "review_id",
        "order_id",
        "review_score",
        "review_comment_title",
        "review_comment_message",
        "review_creation_date",
        "review_answer_timestamp",
        "sentiment_label",
    ],
}


# Olist's own translation file covers 71 of the 73 categories that appear in
# products. These two have no upstream English name; we supply one explicitly
# rather than letting the outer merge leave them NULL, because policy documents
# are titled from the English category name and a NULL there is invisible until
# it reaches an LLM prompt. Declared here so the provenance is obvious.
MANUAL_CATEGORY_TRANSLATIONS = {
    "pc_gamer": "pc_gamer",
    "portateis_cozinha_e_preparadores_de_alimentos": "kitchen_portables_and_food_preparers",
}


COLUMN_RENAMES = {
    "products": {
        "product_name_length": "product_name_lenght",
        "product_description_length": "product_description_lenght",
    },
    "orders": {
        # scripts/clean_data.py emits `shipping_status`; the schema column is
        # `shipment_status`. Not in AUDIT.md; found in M0 and pinned by
        # tests/test_frozen_defects.py. It had been NULL for all 99,441 orders,
        # a third instance of the F-06 mechanism alongside F-04 and F-05.
        "shipping_status": "shipment_status",
    },
    "reviews": {
        # scripts/clean_data.py emits `sentiment`; the schema column is
        # `sentiment_label`. AUDIT.md F-05: this was not declared, so
        # standardize_columns invented sentiment_label as NULL for all
        # 99,224 rows and a B-tree index was built on the empty column.
        "sentiment": "sentiment_label",
    },
}


INTEGER_COLUMNS = {
    "customers": ["customer_zip_code_prefix"],
    "sellers": ["seller_zip_code_prefix"],
    "products": [
        "product_name_lenght",
        "product_description_lenght",
        "product_photos_qty",
        "product_weight_g",
        "product_length_cm",
        "product_height_cm",
        "product_width_cm",
    ],
    "order_items": ["order_item_id"],
    "payments": ["payment_sequential", "payment_installments"],
    "reviews": ["review_score"],
}


NUMERIC_COLUMNS = {
    "order_items": ["price", "freight_value"],
    "payments": ["payment_value"],
}


TIMESTAMP_COLUMNS = {
    "orders": [
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ],
    "order_items": ["shipping_limit_date"],
    "reviews": ["review_creation_date", "review_answer_timestamp"],
}


LOAD_ORDER = [
    "customers",
    "sellers",
    "product_category_translations",
    "products",
    "orders",
    "order_items",
    "payments",
    "reviews",
]


def resolve_dataset_path(data_dir: Path, dataset_name: str, required: bool = True) -> Path | None:
    candidates = FILE_CANDIDATES.get(dataset_name, [])

    for candidate in candidates:
        path = data_dir / candidate
        if path.exists():
            return path

    csv_files = list(data_dir.glob("*.csv"))
    matching_files = [
        path for path in csv_files
        if dataset_name.lower() in path.name.lower()
    ]

    if len(matching_files) == 1:
        return matching_files[0]

    if len(matching_files) > 1:
        names = [path.name for path in matching_files]
        raise ValueError(
            f"Multiple files matched dataset '{dataset_name}': {names}. "
            f"Rename the cleaned file clearly or update FILE_CANDIDATES."
        )

    if required:
        raise FileNotFoundError(
            f"No CSV file found for dataset '{dataset_name}' in {data_dir}."
        )

    return None


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


class SchemaDriftError(RuntimeError):
    """A source file does not carry the columns the target table requires."""


def standardize_columns(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    """Project a source frame onto its table's columns, failing on drift.

    AUDIT.md F-06: this function used to create any missing column as None
    with no log, no warning and no failure. That single construct was the
    delivery mechanism for F-04 (product_category_name_english NULL in 73/73
    rows) and F-05 (sentiment_label NULL in 99,224/99,224), and it would have
    swallowed every future schema change the same way. Missing columns are now
    an error naming what was expected, what is missing, and what the file
    actually provides.
    """
    rename_map = COLUMN_RENAMES.get(table_name, {})
    df = df.rename(columns=rename_map)

    expected_columns = TABLE_COLUMNS[table_name]
    missing = [column for column in expected_columns if column not in df.columns]

    if missing:
        raise SchemaDriftError(
            f"Table '{table_name}' is missing {len(missing)} required column(s): "
            f"{missing}. Columns present in the source file: {sorted(df.columns)}. "
            f"Declare a rename in COLUMN_RENAMES or fix the upstream file — "
            f"this column will NOT be silently created as NULL."
        )

    return df[expected_columns].copy()


def clean_empty_values(df: pd.DataFrame) -> pd.DataFrame:
    return df.replace({"": None})


def convert_integer_columns(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    for column in INTEGER_COLUMNS.get(table_name, []):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")

    return df


def convert_numeric_columns(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    for column in NUMERIC_COLUMNS.get(table_name, []):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    return df


def convert_timestamp_columns(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    for column in TIMESTAMP_COLUMNS.get(table_name, []):
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], errors="coerce")

    return df


def prepare_table_dataframe(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    df = standardize_columns(table_name, df)
    df = clean_empty_values(df)
    df = convert_integer_columns(table_name, df)
    df = convert_numeric_columns(table_name, df)
    df = convert_timestamp_columns(table_name, df)
    return df


def build_engine() -> Engine:
    load_dotenv()

    db_name = os.getenv("POSTGRES_DB", "enterprise_ai")
    db_user = os.getenv("POSTGRES_USER", "enterprise_user")
    db_password = os.getenv("POSTGRES_PASSWORD", "")
    db_port = os.getenv("POSTGRES_PORT", "5432")
    db_host = os.getenv("POSTGRES_HOST", "localhost")

    connection_url = (
        f"postgresql+psycopg2://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}"
    )

    return create_engine(connection_url)


def truncate_tables(engine: Engine) -> None:
    truncate_sql = """
    TRUNCATE TABLE
        ecommerce.reviews,
        ecommerce.payments,
        ecommerce.order_items,
        ecommerce.orders,
        ecommerce.products,
        ecommerce.product_category_translations,
        ecommerce.sellers,
        ecommerce.customers
    RESTART IDENTITY CASCADE;
    """

    with engine.begin() as connection:
        connection.execute(text(truncate_sql))


def load_table(engine: Engine, table_name: str, df: pd.DataFrame) -> int:
    df.to_sql(
        name=table_name,
        con=engine,
        schema="ecommerce",
        if_exists="append",
        index=False,
        chunksize=5000,
        method="multi",
    )

    return len(df)


def get_database_row_counts(engine: Engine) -> dict[str, int]:
    counts = {}

    with engine.begin() as connection:
        for table_name in LOAD_ORDER:
            result = connection.execute(
                text(f"SELECT COUNT(*) FROM ecommerce.{table_name};")
            )
            counts[table_name] = int(result.scalar_one())

    return counts


def load_product_category_translations(
    data_dir: Path,
    products_df: pd.DataFrame,
) -> tuple[pd.DataFrame, str | None]:
    # required=True: AUDIT.md F-04 -- resolving this to None built an empty
    # frame, and the outer merge then filled all 73 English names with NULL.
    # The corpus links policies to sellers by category name, so a NULL here
    # silently produces 73 policies titled "... : None".
    translation_path = resolve_dataset_path(
        data_dir,
        "product_category_translations",
        required=True,
    )

    translations_df = read_csv(translation_path)
    translations_df = prepare_table_dataframe(
        "product_category_translations",
        translations_df,
    )
    source_file = str(translation_path)

    product_categories = (
        products_df["product_category_name"]
        .dropna()
        .astype(str)
        .str.strip()
    )

    product_categories = product_categories[product_categories != ""].drop_duplicates()

    product_category_df = pd.DataFrame({
        "product_category_name": product_categories,
    })

    translations_df = translations_df.merge(
        product_category_df,
        on="product_category_name",
        how="outer",
    )

    if "product_category_name_english" not in translations_df.columns:
        translations_df["product_category_name_english"] = None

    translations_df = translations_df[
        ["product_category_name", "product_category_name_english"]
    ].drop_duplicates(subset=["product_category_name"])

    translations_df = translations_df.dropna(subset=["product_category_name"])

    # Fill the two categories Olist never translated, then assert none are left.
    translations_df["product_category_name_english"] = translations_df.apply(
        lambda row: (
            row["product_category_name_english"]
            if pd.notna(row["product_category_name_english"])
            else MANUAL_CATEGORY_TRANSLATIONS.get(row["product_category_name"])
        ),
        axis=1,
    )

    untranslated = sorted(
        translations_df.loc[
            translations_df["product_category_name_english"].isna(),
            "product_category_name",
        ]
    )

    if untranslated:
        raise SchemaDriftError(
            f"{len(untranslated)} product categories have no English name: "
            f"{untranslated}. AUDIT.md F-04 -- these used to pass through as NULL "
            f"and reach LLM prompts as 'None'. Add them to "
            f"MANUAL_CATEGORY_TRANSLATIONS or fix the translation file."
        )

    return translations_df, source_file


def load_postgres(
    data_dir: Path,
    output_path: Path,
    truncate: bool,
) -> dict[str, Any]:
    engine = build_engine()

    raw_dataframes: dict[str, pd.DataFrame] = {}
    prepared_dataframes: dict[str, pd.DataFrame] = {}
    source_files: dict[str, str | None] = {}

    for table_name in LOAD_ORDER:
        if table_name == "product_category_translations":
            continue

        dataset_path = resolve_dataset_path(data_dir, table_name, required=True)
        assert dataset_path is not None

        raw_df = read_csv(dataset_path)
        prepared_df = prepare_table_dataframe(table_name, raw_df)

        raw_dataframes[table_name] = raw_df
        prepared_dataframes[table_name] = prepared_df
        source_files[table_name] = str(dataset_path)

    translations_df, translation_source = load_product_category_translations(
        data_dir=data_dir,
        products_df=prepared_dataframes["products"],
    )

    prepared_dataframes["product_category_translations"] = translations_df
    source_files["product_category_translations"] = translation_source

    if truncate:
        truncate_tables(engine)

    load_results = {}

    for table_name in LOAD_ORDER:
        df = prepared_dataframes[table_name]
        inserted_rows = load_table(engine, table_name, df)

        load_results[table_name] = {
            "source_file": source_files.get(table_name),
            "input_rows": int(len(df)),
            "inserted_rows": int(inserted_rows),
            "status": "LOADED",
        }

    database_row_counts = get_database_row_counts(engine)

    for table_name, loaded_info in load_results.items():
        loaded_info["database_rows_after_load"] = database_row_counts[table_name]
        loaded_info["row_count_match"] = (
            loaded_info["inserted_rows"] == database_row_counts[table_name]
        )

    failed_tables = [
        table_name
        for table_name, result in load_results.items()
        if not result["row_count_match"]
    ]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "truncate_before_load": truncate,
        "summary": {
            "table_count": len(LOAD_ORDER),
            "failed_tables": failed_tables,
            "overall_status": "PASS" if not failed_tables else "FAIL",
        },
        "tables": load_results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load cleaned CSV data into PostgreSQL ecommerce schema."
    )

    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/processed"),
        help="Directory containing cleaned CSV files.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/postgres_load_report.json"),
        help="Path to save the PostgreSQL load report.",
    )

    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate ecommerce tables before loading data.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = load_postgres(
        data_dir=args.data_dir,
        output_path=args.output,
        truncate=args.truncate,
    )

    print("\nPostgreSQL Load Completed")
    print("-------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Tables loaded: {report['summary']['table_count']}")
    print(f"Report saved to: {args.output}")

    for table_name, result in report["tables"].items():
        print(
            f"{table_name}: "
            f"{result['inserted_rows']} inserted, "
            f"{result['database_rows_after_load']} in database"
        )

    if report["summary"]["failed_tables"]:
        print(f"Failed tables: {report['summary']['failed_tables']}")


if __name__ == "__main__":
    main()