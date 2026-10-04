from pathlib import Path

import duckdb
import pytest

from atalayero.ingestion.load_batch import csv_to_parquet, load_parquet_to_duckdb
from atalayero.ingestion.patterns import (
    LaunderingAttempt,
    PatternsFormatError,
    load_laundering_attempts,
    parse_patterns,
)

FAN_OUT_BEGIN = "BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 2-degree Fan-Out"
FAN_OUT_END = "END LAUNDERING ATTEMPT - FAN-OUT"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "patterns.txt"
    path.write_text(text)
    return path


def test_parses_blocks(tmp_path: Path) -> None:
    text = (
        f"{FAN_OUT_BEGIN}\nrow 1\nrow 2\n{FAN_OUT_END}\n\n"
        "BEGIN LAUNDERING ATTEMPT - SCATTER-GATHER\nrow 3\n"
        "END LAUNDERING ATTEMPT - SCATTER-GATHER\n\n"
    )

    attempts = parse_patterns(_write(tmp_path, text))

    assert attempts == [
        LaunderingAttempt(
            attempt_id=1,
            typology="fan_out",
            description="Max 2-degree Fan-Out",
            rows=("row 1", "row 2"),
            block=(FAN_OUT_BEGIN, "row 1", "row 2", FAN_OUT_END),
        ),
        LaunderingAttempt(
            attempt_id=2,
            typology="scatter_gather",
            description="",
            rows=("row 3",),
            block=(
                "BEGIN LAUNDERING ATTEMPT - SCATTER-GATHER",
                "row 3",
                "END LAUNDERING ATTEMPT - SCATTER-GATHER",
            ),
        ),
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("row\n", "transaction outside an attempt"),
        (f"{FAN_OUT_BEGIN}\nrow\n", "unterminated attempt"),
        (f"{FAN_OUT_BEGIN}\nrow\n{FAN_OUT_BEGIN}\n", "BEGIN inside an open attempt"),
        (f"{FAN_OUT_BEGIN}\nrow\nEND LAUNDERING ATTEMPT - FAN-IN\n", "END without a matching"),
        (f"{FAN_OUT_BEGIN}\nrow\n\nrow\n{FAN_OUT_END}\n", "blank line inside an attempt"),
        (
            "BEGIN LAUNDERING ATTEMPT - SMURFING\nrow\nEND LAUNDERING ATTEMPT - SMURFING\n",
            "unknown typology",
        ),
    ],
)
def test_rejects_malformed_files(tmp_path: Path, text: str, error: str) -> None:
    with pytest.raises(PatternsFormatError, match=error):
        parse_patterns(_write(tmp_path, text))


@pytest.fixture
def db_with_transactions(sample_csv: Path, tmp_path: Path) -> Path:
    db = tmp_path / "atalayero.duckdb"
    csv_to_parquet(sample_csv, tmp_path / "transactions.parquet")
    load_parquet_to_duckdb(tmp_path / "transactions.parquet", db)
    return db


def test_maps_every_pattern_line_to_one_laundering_transaction(
    sample_patterns: Path, db_with_transactions: Path
) -> None:
    attempts = parse_patterns(sample_patterns)
    expected = sum(len(a.rows) for a in attempts)

    assert load_laundering_attempts(sample_patterns, db_with_transactions) == expected

    with duckdb.connect(str(db_with_transactions), read_only=True) as con:
        summary = con.execute(
            """
            SELECT count(*), count(DISTINCT transaction_id), count(DISTINCT attempt_id),
                   count(DISTINCT typology), bool_and(t.is_laundering)
            FROM raw.laundering_attempts JOIN raw.transactions t USING (transaction_id)
            """
        ).fetchone()
    assert summary == (expected, expected, len(attempts), 8, True)


def test_unmatched_line_fails_and_keeps_the_previous_table(
    sample_patterns: Path, db_with_transactions: Path, tmp_path: Path
) -> None:
    loaded = load_laundering_attempts(sample_patterns, db_with_transactions)
    unknown = "2022/09/01 00:00,000,NOPE,000,NOPE,1.00,Euro,1.00,Euro,ACH,1"
    bad = _write(tmp_path, f"{FAN_OUT_BEGIN}\n{unknown}\n{FAN_OUT_END}\n\n")

    with pytest.raises(ValueError, match="1 pattern lines do not match exactly one transaction"):
        load_laundering_attempts(bad, db_with_transactions)

    with duckdb.connect(str(db_with_transactions), read_only=True) as con:
        (rows,) = con.execute("SELECT count(*) FROM raw.laundering_attempts").fetchone()
    assert rows == loaded
