"""Batch load into the DuckDB `raw` schema: transactions (via Parquet) and laundering attempts."""

import logging
from pathlib import Path

import duckdb

from atalayero.ingestion.patterns import load_laundering_attempts
from atalayero.ingestion.source import SOURCE_COLUMNS, SOURCE_TIMESTAMP_FORMAT, sql_literal
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

TRANSACTIONS_PARQUET = "transactions.parquet"


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


def load_batch(
    settings: Settings,
    transactions_csv: Path | None = None,
    patterns_txt: Path | None = None,
) -> int:
    """Load `raw.transactions` and `raw.laundering_attempts`; return the transaction count.

    Reads the downloaded files unless others are given (e.g. the test fixtures).
    """
    transactions_csv = transactions_csv or settings.raw_dir / settings.dataset.transactions.name
    patterns_txt = patterns_txt or settings.raw_dir / settings.dataset.patterns.name
    parquet_path = settings.raw_dir / TRANSACTIONS_PARQUET
    csv_to_parquet(transactions_csv, parquet_path)
    rows = load_parquet_to_duckdb(parquet_path, settings.duckdb_path)
    load_laundering_attempts(patterns_txt, settings.duckdb_path)
    return rows
