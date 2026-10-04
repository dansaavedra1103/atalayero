from pathlib import Path

import duckdb
import pytest

from atalayero.ingestion.fx_rates import derive_fx_rates, write_fx_seed


def _raw_db(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    """raw.transactions with only the columns the derivation reads: (paid, currency, received,
    currency)."""
    db = tmp_path / "atalayero.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE SCHEMA raw")
        con.execute(
            "CREATE TABLE raw.transactions (amount_paid DECIMAL(20, 6), payment_currency VARCHAR, "
            "amount_received DECIMAL(20, 6), receiving_currency VARCHAR)"
        )
        con.executemany("INSERT INTO raw.transactions VALUES (?, ?, ?, ?)", rows)
    return db


def test_rate_is_the_median_over_both_directions(tmp_path: Path) -> None:
    db = _raw_db(
        tmp_path,
        [
            ("100.00", "Euro", "117.18", "US Dollar"),
            ("234.36", "US Dollar", "200.00", "Euro"),
            ("0.01", "Euro", "0.01", "US Dollar"),  # rounded to cents: outvoted by the median
            ("50.00", "US Dollar", "50.00", "US Dollar"),
        ],
    )

    rates = derive_fx_rates(db)

    assert [(c, n) for c, _, n in rates] == [("Euro", 3), ("US Dollar", 0)]
    assert rates[0][1] == pytest.approx(1.1718)
    assert rates[1][1] == 1.0


def test_currency_without_a_us_dollar_pair_fails(tmp_path: Path) -> None:
    db = _raw_db(
        tmp_path,
        [("100.00", "Euro", "117.18", "US Dollar"), ("10.00", "Yen", "10.00", "Yen")],
    )

    with pytest.raises(ValueError, match="Yen"):
        derive_fx_rates(db)


def test_writes_the_seed(tmp_path: Path) -> None:
    db = _raw_db(tmp_path, [("100.00", "Euro", "117.18", "US Dollar")])
    seed = tmp_path / "seeds" / "fx_rates_usd.csv"

    assert write_fx_seed(db, seed) == 2
    assert seed.read_text().splitlines() == [
        "currency,usd_per_unit,observations",
        "Euro,1.1718,1",
        "US Dollar,1,0",
    ]
