"""Check that scripts/clean_data.py reproduces the data that is actually in PostgreSQL.

M0 task 3. clean_data.py is a transcription of a notebook that was never in the
repository, so "it looks right" is not evidence. This script regenerates the cleaned
CSVs into a scratch directory, pushes each one through the loader's own
prepare_table_dataframe() -- so CSV and database are compared after exactly the
transformations the loader applies -- and then compares against the live tables on:

  * row count
  * md5 over the sorted primary key
  * sum / min / max of every numeric and integer column
  * null count and md5 over sorted distinct values of every text column

Exit code is 0 only if every table matches on every measure.

    python scripts/verify_clean_data.py
    python scripts/verify_clean_data.py --scratch-dir /tmp/check --json out.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "database"))

from postgres_loader import (  # noqa: E402
    INTEGER_COLUMNS,
    NUMERIC_COLUMNS,
    TABLE_COLUMNS,
    TIMESTAMP_COLUMNS,
    prepare_table_dataframe,
    read_csv,
)

# Table -> the cleaned CSV that feeds it. product_category_translations is absent on
# purpose: the loader could not resolve a file for it and synthesised the table from
# the distinct product categories instead (reports/postgres_load_report.json records
# "source_file": null). That case is checked separately below.
TABLE_SOURCES = {
    "customers": "customers_cleaned.csv",
    "sellers": "sellers_cleaned.csv",
    "products": "products_cleaned.csv",
    "orders": "orders_cleaned.csv",
    "order_items": "order_items_cleaned.csv",
    "payments": "payments_cleaned.csv",
    "reviews": "reviews_cleaned.csv",
}

PRIMARY_KEYS = {
    "customers": ["customer_id"],
    "sellers": ["seller_id"],
    "products": ["product_id"],
    "orders": ["order_id"],
    "order_items": ["order_id", "order_item_id"],
    "payments": ["order_id", "payment_sequential"],
    "reviews": ["review_id", "order_id"],
}


def build_engine():
    load_dotenv(PROJECT_ROOT / ".env")
    url = (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'enterprise_user')}:"
        f"{os.getenv('POSTGRES_PASSWORD', 'enterprise_password')}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}/"
        f"{os.getenv('POSTGRES_DB', 'enterprise_ai')}"
    )
    return create_engine(url)


def _md5(values) -> str:
    digest = hashlib.md5()
    for value in values:
        digest.update(str(value).encode("utf-8", errors="surrogatepass"))
        digest.update(b"\x00")
    return digest.hexdigest()


def fingerprint(table: str, df: pd.DataFrame) -> dict[str, Any]:
    """Reduce a table to a set of measures comparable across pandas and Postgres."""
    numeric = set(NUMERIC_COLUMNS.get(table, [])) | set(INTEGER_COLUMNS.get(table, []))
    timestamps = set(TIMESTAMP_COLUMNS.get(table, []))

    out: dict[str, Any] = {"row_count": int(len(df))}

    keys = PRIMARY_KEYS[table]
    key_series = df[keys[0]].astype(str)
    for extra in keys[1:]:
        key_series = key_series + "|" + df[extra].astype(str)
    out["pk_md5"] = _md5(sorted(key_series.tolist()))

    columns: dict[str, Any] = {}
    for column in TABLE_COLUMNS[table]:
        series = df[column]
        entry: dict[str, Any] = {"nulls": int(series.isna().sum())}
        if column in numeric:
            values = pd.to_numeric(series, errors="coerce")
            entry["sum"] = None if values.notna().sum() == 0 else round(float(values.sum()), 2)
            entry["min"] = None if values.notna().sum() == 0 else float(values.min())
            entry["max"] = None if values.notna().sum() == 0 else float(values.max())
        elif column in timestamps:
            values = pd.to_datetime(series, errors="coerce")
            entry["min"] = None if values.notna().sum() == 0 else str(values.min())
            entry["max"] = None if values.notna().sum() == 0 else str(values.max())
        else:
            present = series.dropna().astype(str)
            entry["distinct"] = int(present.nunique())
            entry["distinct_md5"] = _md5(sorted(present.unique().tolist()))
        columns[column] = entry
    out["columns"] = columns
    return out


def read_database_table(engine, table: str) -> pd.DataFrame:
    columns = ", ".join(f'"{c}"' for c in TABLE_COLUMNS[table])
    with engine.connect() as connection:
        rows = connection.execute(text(f"SELECT {columns} FROM ecommerce.{table}")).fetchall()
    return pd.DataFrame(rows, columns=TABLE_COLUMNS[table])


def diff(expected: dict[str, Any], actual: dict[str, Any], path: str = "") -> list[str]:
    problems: list[str] = []
    for key in expected:
        here = f"{path}.{key}" if path else key
        left, right = expected[key], actual.get(key)
        if isinstance(left, dict):
            problems.extend(diff(left, right or {}, here))
        elif left != right:
            problems.append(f"{here}: csv={left!r} db={right!r}")
    return problems


def check_translations(engine, scratch: Path) -> tuple[bool, list[str]]:
    """Every category must carry an English name (M1 fixed AUDIT.md F-04)."""
    products = read_csv(scratch / "products_cleaned.csv")
    expected = sorted(products["product_category_name"].dropna().unique().tolist())
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT product_category_name, product_category_name_english "
                "FROM ecommerce.product_category_translations"
            )
        ).fetchall()
    actual = sorted(r[0] for r in rows)
    english_present = sum(1 for r in rows if r[1] is not None)
    problems = []
    if actual != expected:
        problems.append(
            f"category set differs: csv has {len(expected)}, db has {len(actual)}"
        )
    if english_present != len(rows):
        problems.append(
            f"expected an english name for all {len(rows)} categories "
            f"(AUDIT.md F-04, fixed in M1), found {english_present}"
        )
    return not problems, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify clean_data.py against PostgreSQL.")
    parser.add_argument("--scratch-dir", default=None)
    parser.add_argument("--json", default=None)
    parser.add_argument(
        "--skip-regenerate",
        action="store_true",
        help="use the CSVs already in --scratch-dir instead of running clean_data.py",
    )
    args = parser.parse_args()

    scratch = Path(args.scratch_dir) if args.scratch_dir else Path(tempfile.mkdtemp(prefix="cleancheck_"))
    scratch.mkdir(parents=True, exist_ok=True)

    if not args.skip_regenerate:
        print(f"regenerating cleaned CSVs into {scratch} ...")
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "clean_data.py"),
             "--output-dir", str(scratch)],
            check=True,
            cwd=PROJECT_ROOT,
        )
        print()

    engine = build_engine()
    results: dict[str, Any] = {}
    failures = 0

    for table, filename in TABLE_SOURCES.items():
        csv_df = prepare_table_dataframe(table, read_csv(scratch / filename))
        db_df = read_database_table(engine, table)
        csv_fp = fingerprint(table, csv_df)
        db_fp = fingerprint(table, db_df)
        problems = diff(csv_fp, db_fp)
        results[table] = {
            "rows_csv": csv_fp["row_count"],
            "rows_db": db_fp["row_count"],
            "pk_md5_csv": csv_fp["pk_md5"],
            "pk_md5_db": db_fp["pk_md5"],
            "match": not problems,
            "problems": problems,
        }
        failures += bool(problems)
        status = "MATCH" if not problems else f"MISMATCH ({len(problems)})"
        print(
            f"{table:<32} rows {csv_fp['row_count']:>7} | pk_md5 "
            f"{csv_fp['pk_md5'][:16]} | {status}"
        )
        for problem in problems:
            print(f"    {problem}")

    ok, problems = check_translations(engine, scratch)
    failures += not ok
    results["product_category_translations"] = {"match": ok, "problems": problems}
    print(f"{'product_category_translations':<32} {'MATCH (synthesised from products)' if ok else 'MISMATCH'}")
    for problem in problems:
        print(f"    {problem}")

    print()
    print("OVERALL:", "MATCH" if failures == 0 else f"{failures} TABLE(S) MISMATCHED")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"json written to {args.json}")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
