import csv
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from atalayero.ingestion.load_batch import csv_to_parquet, load_batch, load_parquet_to_duckdb
from atalayero.ingestion.source import SOURCE_COLUMNS
from atalayero.settings import Settings

REPO_ROOT = Path(__file__).parents[2]


def _source_rows(path: Path) -> list[list[str]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        next(reader)
        return list(reader)


def _parquet_rows(path: Path) -> list[tuple]:
    return duckdb.sql(f"SELECT * FROM read_parquet('{path}') ORDER BY transaction_id").fetchall()


def test_parquet_keeps_every_source_value_exact(sample_csv: Path, tmp_path: Path) -> None:
    source = _source_rows(sample_csv)
    # The fixture must cover the tricky values for this test to mean anything.
    assert any(row[1].startswith("0") for row in source), "no bank ID with leading zeros"
    assert any(len(row[5].split(".")[1]) == 6 for row in source), "no 6-decimal amount"

    rows = csv_to_parquet(sample_csv, tmp_path / "transactions.parquet")

    assert rows == len(source)
    loaded = _parquet_rows(tmp_path / "transactions.parquet")
    for transaction_id, (got, src) in enumerate(zip(loaded, source, strict=True), start=1):
        expected = (
            transaction_id,  # 1-based row number in the source file
            datetime.strptime(src[0], "%Y/%m/%d %H:%M"),
            *src[1:5],
            Decimal(src[5]),
            src[6],
            Decimal(src[7]),
            src[8],
            src[9],
            src[10] == "1",
        )
        assert got == expected


def test_parquet_schema(sample_csv: Path, tmp_path: Path) -> None:
    parquet = tmp_path / "transactions.parquet"
    csv_to_parquet(sample_csv, parquet)

    described = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{parquet}')").fetchall()

    expected = {"transaction_id": "BIGINT"} | SOURCE_COLUMNS
    assert {name: type_ for name, type_, *_ in described} == {
        name: type_.replace(" ", "") for name, type_ in expected.items()
    }


def test_malformed_csv_fails_the_load(sample_csv: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    header = sample_csv.read_text().splitlines()[0]
    bad.write_text(f"{header}\n2022/09/01 00:20,010,A,010,B,not-a-number,Euro,1.00,Euro,ACH,0\n")

    with pytest.raises(duckdb.Error):
        csv_to_parquet(bad, tmp_path / "bad.parquet")


def test_duckdb_load_is_idempotent(sample_csv: Path, tmp_path: Path) -> None:
    parquet = tmp_path / "transactions.parquet"
    db = tmp_path / "db" / "atalayero.duckdb"
    rows = csv_to_parquet(sample_csv, parquet)

    assert load_parquet_to_duckdb(parquet, db) == rows
    assert load_parquet_to_duckdb(parquet, db) == rows

    with duckdb.connect(str(db), read_only=True) as con:
        counts = con.execute(
            "SELECT count(*), count(DISTINCT transaction_id) FROM raw.transactions"
        ).fetchone()
    assert counts == (rows, rows)


def test_load_batch_end_to_end(
    sample_csv: Path, sample_patterns: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)  # settings.yaml is resolved from the repository root
    settings = Settings(data_dir=tmp_path, duckdb_path=tmp_path / "atalayero.duckdb")
    settings.raw_dir.mkdir()
    shutil.copy(sample_csv, settings.raw_dir / settings.dataset.transactions.name)
    shutil.copy(sample_patterns, settings.raw_dir / settings.dataset.patterns.name)

    assert load_batch(settings) == len(_source_rows(sample_csv))
    assert (settings.raw_dir / "transactions.parquet").exists()
    with duckdb.connect(str(settings.duckdb_path), read_only=True) as con:
        (attempts,) = con.execute(
            "SELECT count(DISTINCT attempt_id) FROM raw.laundering_attempts"
        ).fetchone()
    assert attempts == 8


def test_load_batch_from_explicit_files(
    sample_csv: Path, sample_patterns: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    settings = Settings(data_dir=tmp_path, duckdb_path=tmp_path / "atalayero.duckdb")

    assert load_batch(settings, sample_csv, sample_patterns) == len(_source_rows(sample_csv))
