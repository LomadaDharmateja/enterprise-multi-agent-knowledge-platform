from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


FILE_CANDIDATES = {
    "customers": [
        "customers.csv",
        "customers_clean.csv",
        "customers_cleaned.csv",
        "olist_customers_dataset.csv",
        "olist_customers_cleaned.csv",
    ],
    "orders": [
        "orders.csv",
        "orders_clean.csv",
        "orders_cleaned.csv",
        "olist_orders_dataset.csv",
        "olist_orders_cleaned.csv",
    ],
    "products": [
        "products.csv",
        "products_clean.csv",
        "products_cleaned.csv",
        "olist_products_dataset.csv",
        "olist_products_cleaned.csv",
    ],
    "sellers": [
        "sellers.csv",
        "sellers_clean.csv",
        "sellers_cleaned.csv",
        "olist_sellers_dataset.csv",
        "olist_sellers_cleaned.csv",
    ],
    "order_items": [
        "order_items.csv",
        "order_items_clean.csv",
        "order_items_cleaned.csv",
        "olist_order_items_dataset.csv",
        "olist_order_items_cleaned.csv",
    ],
    "payments": [
        "payments.csv",
        "payments_clean.csv",
        "payments_cleaned.csv",
        "order_payments.csv",
        "order_payments_cleaned.csv",
        "olist_order_payments_dataset.csv",
        "olist_order_payments_cleaned.csv",
    ],
    "reviews": [
        "reviews.csv",
        "reviews_clean.csv",
        "reviews_cleaned.csv",
        "order_reviews.csv",
        "order_reviews_cleaned.csv",
        "olist_order_reviews_dataset.csv",
        "olist_order_reviews_cleaned.csv",
    ],
}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Contract file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def resolve_dataset_path(data_dir: Path, source_name: str) -> Path:
    candidates = FILE_CANDIDATES.get(source_name, [])

    for candidate in candidates:
        path = data_dir / candidate
        if path.exists():
            return path

    csv_files = list(data_dir.glob("*.csv"))
    matching_files = [
        path for path in csv_files
        if source_name.lower() in path.name.lower()
    ]

    if len(matching_files) == 1:
        return matching_files[0]

    if len(matching_files) > 1:
        names = [path.name for path in matching_files]
        raise ValueError(
            f"Multiple possible files found for source '{source_name}': {names}. "
            f"Please rename the cleaned file clearly or update FILE_CANDIDATES."
        )

    raise FileNotFoundError(
        f"No CSV file found for source '{source_name}' in {data_dir}. "
        f"Expected one of: {candidates}"
    )


def read_dataset(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def normalize_key_list(key_config: str | list[str]) -> list[str]:
    if isinstance(key_config, list):
        return key_config
    return [key_config]


def missing_key_mask(df: pd.DataFrame, keys: list[str]) -> pd.Series:
    mask = pd.Series(False, index=df.index)

    for key in keys:
        values = df[key].astype(str).str.strip()
        mask = mask | values.eq("")

    return mask


def make_key_series(df: pd.DataFrame, keys: list[str]) -> pd.Series:
    if len(keys) == 1:
        return df[keys[0]].astype(str).str.strip()

    return df[keys].astype(str).apply(
        lambda row: "||".join(value.strip() for value in row),
        axis=1,
    )


def check_entity(
    entity_name: str,
    entity_config: dict[str, Any],
    df: pd.DataFrame,
    dataset_path: Path,
) -> dict[str, Any]:
    primary_keys = normalize_key_list(entity_config["primary_key"])

    missing_columns = [
        column for column in primary_keys
        if column not in df.columns
    ]

    result: dict[str, Any] = {
        "entity": entity_name,
        "source": entity_config["source"],
        "dataset_file": str(dataset_path),
        "row_count": int(len(df)),
        "primary_key": primary_keys,
        "missing_primary_key_columns": missing_columns,
        "primary_key_null_or_empty_rows": None,
        "duplicate_primary_key_rows": None,
        "unique_primary_key_count": None,
        "status": "FAIL",
    }

    if missing_columns:
        return result

    null_or_empty_mask = missing_key_mask(df, primary_keys)
    valid_key_df = df.loc[~null_or_empty_mask].copy()

    duplicate_mask = valid_key_df.duplicated(subset=primary_keys, keep=False)

    result["primary_key_null_or_empty_rows"] = int(null_or_empty_mask.sum())
    result["duplicate_primary_key_rows"] = int(duplicate_mask.sum())
    result["unique_primary_key_count"] = int(
        valid_key_df[primary_keys].drop_duplicates().shape[0]
    )

    if (
        result["primary_key_null_or_empty_rows"] == 0
        and result["duplicate_primary_key_rows"] == 0
    ):
        result["status"] = "PASS"

    return result


def get_parent_child_sides(
    relationship_config: dict[str, Any],
) -> tuple[str, str, str, str]:
    cardinality = relationship_config["cardinality"]

    from_entity = relationship_config["from_entity"]
    to_entity = relationship_config["to_entity"]
    from_key = relationship_config["from_key"]
    to_key = relationship_config["to_key"]

    if cardinality in {"one_to_many", "one_to_many_or_zero", "one_to_one_or_zero"}:
        parent_entity = from_entity
        parent_key = from_key
        child_entity = to_entity
        child_key = to_key
    elif cardinality == "many_to_one":
        parent_entity = to_entity
        parent_key = to_key
        child_entity = from_entity
        child_key = from_key
    else:
        raise ValueError(f"Unsupported cardinality: {cardinality}")

    return parent_entity, parent_key, child_entity, child_key


def check_relationship(
    relationship_name: str,
    relationship_config: dict[str, Any],
    entity_configs: dict[str, Any],
    datasets: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    parent_entity, parent_key, child_entity, child_key = get_parent_child_sides(
        relationship_config
    )

    parent_source = entity_configs[parent_entity]["source"]
    child_source = entity_configs[child_entity]["source"]

    parent_df = datasets[parent_source]
    child_df = datasets[child_source]

    result: dict[str, Any] = {
        "relationship": relationship_name,
        "business_meaning": relationship_config.get("business_meaning"),
        "cardinality": relationship_config["cardinality"],
        "parent_entity": parent_entity,
        "child_entity": child_entity,
        "parent_key": parent_key,
        "child_key": child_key,
        "parent_row_count": int(len(parent_df)),
        "child_row_count": int(len(child_df)),
        "missing_columns": [],
        "child_rows_with_missing_foreign_key": None,
        "orphan_child_rows": None,
        "orphan_child_distinct_keys": None,
        "matched_child_rows": None,
        "coverage_percent": None,
        "parent_records_without_child": None,
        "max_children_per_parent": None,
        "status": "FAIL",
    }

    if parent_key not in parent_df.columns:
        result["missing_columns"].append(
            {"entity": parent_entity, "missing_column": parent_key}
        )

    if child_key not in child_df.columns:
        result["missing_columns"].append(
            {"entity": child_entity, "missing_column": child_key}
        )

    if result["missing_columns"]:
        return result

    parent_keys = parent_df[parent_key].astype(str).str.strip()
    child_keys = child_df[child_key].astype(str).str.strip()

    child_missing_fk_mask = child_keys.eq("")
    valid_child_keys = child_keys.loc[~child_missing_fk_mask]

    parent_key_set = set(parent_keys[parent_keys.ne("")].unique())

    orphan_mask = ~valid_child_keys.isin(parent_key_set)

    matched_child_rows = int((~orphan_mask).sum())
    orphan_child_rows = int(orphan_mask.sum())
    valid_child_row_count = int(len(valid_child_keys))

    coverage_percent = (
        round((matched_child_rows / valid_child_row_count) * 100, 4)
        if valid_child_row_count > 0
        else 0.0
    )

    child_key_counts = valid_child_keys.value_counts()
    parent_records_without_child = int(
        len(set(parent_keys[parent_keys.ne("")].unique()) - set(valid_child_keys.unique()))
    )

    result["child_rows_with_missing_foreign_key"] = int(child_missing_fk_mask.sum())
    result["orphan_child_rows"] = orphan_child_rows
    result["orphan_child_distinct_keys"] = int(valid_child_keys.loc[orphan_mask].nunique())
    result["matched_child_rows"] = matched_child_rows
    result["coverage_percent"] = coverage_percent
    result["parent_records_without_child"] = parent_records_without_child
    result["max_children_per_parent"] = int(child_key_counts.max()) if not child_key_counts.empty else 0

    if (
        result["child_rows_with_missing_foreign_key"] == 0
        and result["orphan_child_rows"] == 0
    ):
        result["status"] = "PASS"

    return result


def build_summary(
    entity_results: dict[str, Any],
    relationship_results: dict[str, Any],
) -> dict[str, Any]:
    failed_entities = [
        name for name, result in entity_results.items()
        if result["status"] != "PASS"
    ]

    failed_relationships = [
        name for name, result in relationship_results.items()
        if result["status"] != "PASS"
    ]

    return {
        "entity_count": len(entity_results),
        "relationship_count": len(relationship_results),
        "failed_entities": failed_entities,
        "failed_relationships": failed_relationships,
        "overall_status": "PASS" if not failed_entities and not failed_relationships else "FAIL",
    }


def validate_relationships(
    data_dir: Path,
    contract_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    contract = load_yaml(contract_path)

    entity_configs = contract["entities"]
    relationship_configs = contract["relationships"]

    datasets: dict[str, pd.DataFrame] = {}
    dataset_paths: dict[str, Path] = {}

    for entity_config in entity_configs.values():
        source = entity_config["source"]

        if source not in datasets:
            dataset_path = resolve_dataset_path(data_dir, source)
            datasets[source] = read_dataset(dataset_path)
            dataset_paths[source] = dataset_path

    entity_results = {}

    for entity_name, entity_config in entity_configs.items():
        source = entity_config["source"]
        entity_results[entity_name] = check_entity(
            entity_name=entity_name,
            entity_config=entity_config,
            df=datasets[source],
            dataset_path=dataset_paths[source],
        )

    relationship_results = {}

    for relationship_name, relationship_config in relationship_configs.items():
        relationship_results[relationship_name] = check_relationship(
            relationship_name=relationship_name,
            relationship_config=relationship_config,
            entity_configs=entity_configs,
            datasets=datasets,
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract_version": contract.get("version"),
        "data_dir": str(data_dir),
        "summary": build_summary(entity_results, relationship_results),
        "entities": entity_results,
        "relationships": relationship_results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate cleaned enterprise datasets against the relationship contract."
    )

    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/processed"),
        help="Directory containing cleaned CSV datasets.",
    )

    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("configs/relationship_contract.yaml"),
        help="Path to relationship contract YAML file.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/relationship_validation_report.json"),
        help="Path where validation report JSON will be written.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = validate_relationships(
        data_dir=args.data_dir,
        contract_path=args.contract,
        output_path=args.output,
    )

    print("\nRelationship Validation Completed")
    print("--------------------------------")
    print(f"Overall status: {report['summary']['overall_status']}")
    print(f"Entity checks: {report['summary']['entity_count']}")
    print(f"Relationship checks: {report['summary']['relationship_count']}")
    print(f"Report saved to: {args.output}")

    if report["summary"]["failed_entities"]:
        print(f"Failed entities: {report['summary']['failed_entities']}")

    if report["summary"]["failed_relationships"]:
        print(f"Failed relationships: {report['summary']['failed_relationships']}")


if __name__ == "__main__":
    main()