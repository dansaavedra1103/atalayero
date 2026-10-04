"""Build the test fixture: a seeded, stratified sample of the source CSV, kept in its raw format."""

import csv
import logging
from pathlib import Path

import duckdb

from atalayero.ingestion.load_batch import SOURCE_COLUMNS, sql_literal

logger = logging.getLogger(__name__)


def write_sample(
    csv_path: Path,
    out_path: Path,
    n_rows: int = 1000,
    laundering_share: float = 0.05,
    seed: int = 42,
) -> int:
    """Write `n_rows` source rows to `out_path`, byte-for-byte as in the source; return the count.

    Laundering rows are oversampled to `laundering_share` (about 0.1% in the source) so tests see
    positives. Rows keep their source order and the source header is copied verbatim.
    """
    n_laundering = round(n_rows * laundering_share)
    names = ", ".join(sql_literal(name) for name in SOURCE_COLUMNS)
    with duckdb.connect() as con:
        # Same row numbering as load_batch.csv_to_parquet; all_varchar keeps every value exactly
        # as written (leading zeros, decimals).
        con.execute("SET preserve_insertion_order = true")
        con.execute(
            f"""
            CREATE TABLE source AS
            SELECT * FROM read_csv({sql_literal(csv_path)}, header = true, all_varchar = true,
                                   names = [{names}])
            """
        )
        rows = con.execute(
            """
            WITH ranked AS (
                SELECT rowid AS row_id, *, row_number() OVER (
                    PARTITION BY is_laundering ORDER BY hash(rowid, $seed), rowid
                ) AS stratum_rank
                FROM source
            )
            SELECT * EXCLUDE (row_id, stratum_rank)
            FROM ranked
            WHERE stratum_rank <= CASE is_laundering WHEN '1' THEN $n_laundering
                                                     ELSE $n_rows - $n_laundering END
            ORDER BY row_id
            """,
            {"seed": seed, "n_laundering": n_laundering, "n_rows": n_rows},
        ).fetchall()

    with csv_path.open(newline="") as f:
        header = f.readline()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as out:
        out.write(header)
        csv.writer(out, lineterminator="\n").writerows(rows)
    logger.info("Wrote %d sample rows (%d laundering) to %s", len(rows), n_laundering, out_path)
    return len(rows)
