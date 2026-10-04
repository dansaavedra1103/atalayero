"""Batch load: source CSV -> typed Parquet -> DuckDB table `raw.transactions`."""

import logging
from pathlib import Path

import duckdb

from atalayero.settings import Settings

logger = logging.getLogger(__name__)

# The source header repeats "Account" (sender, then receiver), so columns are named explicitly.
# Bank IDs have leading zeros ("010" is not "10"), so they stay VARCHAR.
# Amounts have up to 6 decimals (Bitcoin) and 13 integer digits: DECIMAL(20, 6) is exact.
SOURCE_COLUMNS: dict[str, str] = {
    "timestamp": "TIMESTAMP",
    "from_bank": "VARCHAR",
    "from_account": "VARCHAR",
    "to_bank": "VARCHAR",
    "to_account": "VARCHAR",
    "amount_received": "DECIMAL(20, 6)",
    "receiving_currency": "VARCHAR",
    "amount_paid": "DECIMAL(20, 6)",
    "payment_currency": "VARCHAR",
    "payment_format": "VARCHAR",
    "is_laundering": "BOOLEAN",
}
SOURCE_TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M"
TRANSACTIONS_PARQUET = "transactions.parquet"


def sql_literal(value: object) -> str:
    """Quote a value as a SQL string literal (COPY and DDL statements take no parameters)."""
    return "'" + str(value).replace("'", "''") + "'"


def csv_to_parquet(csv_path: Path, parquet_path: Path) -> int:
    """Write the source CSV as typed Parquet with a `transaction_id` column; return the row count.

    `transaction_id` is the 1-based row number in the source file, header excluded. It is stable
    because the source file is pinned by its SHA-256 (ADR-0001).
    """
    columns = ", ".join(
        f"{sql_literal(name)}: {sql_literal(type_)}" for name, type_ in SOURCE_COLUMNS.items()
    )
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Converting %s -> %s", csv_path, parquet_path)
    with duckdb.connect() as con:
        # With insertion order preserved, a fresh table's rowid is the position in the file.
        # (row_number() OVER () gives the same result but runs single-threaded, ~5x slower.)
        con.execute("SET preserve_insertion_order = true")
        con.execute(
            f"""
            CREATE TABLE source AS
            SELECT * FROM read_csv(
                {sql_literal(csv_path)},
                header = true,
                columns = {{{columns}}},
                timestampformat = {sql_literal(SOURCE_TIMESTAMP_FORMAT)}
            )
            """
        )
        con.execute(
            "COPY (SELECT rowid + 1 AS transaction_id, * FROM source) "
            f"TO {sql_literal(parquet_path)} (FORMAT parquet)"
        )
        (rows,) = con.execute("SELECT count(*) FROM source").fetchone()
    logger.info("Wrote %d rows to %s", rows, parquet_path)
    return rows


def load_parquet_to_duckdb(parquet_path: Path, duckdb_path: Path) -> int:
    """(Re)create `raw.transactions` from the Parquet file; return the row count."""
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(duckdb_path)) as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(
            "CREATE OR REPLACE TABLE raw.transactions AS "
            f"SELECT * FROM read_parquet({sql_literal(parquet_path)})"
        )
        (rows,) = con.execute("SELECT count(*) FROM raw.transactions").fetchone()
    logger.info("Loaded %d rows into raw.transactions (%s)", rows, duckdb_path)
    return rows


def load_batch(settings: Settings) -> int:
    parquet_path = settings.raw_dir / TRANSACTIONS_PARQUET
    csv_to_parquet(settings.raw_dir / settings.dataset.transactions.name, parquet_path)
    return load_parquet_to_duckdb(parquet_path, settings.duckdb_path)
