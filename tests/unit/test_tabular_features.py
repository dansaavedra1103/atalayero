import random
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from atalayero.features.tabular import FEATURES, build_tabular_features, tabular_features_sql
from atalayero.schemas import Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]

REPO_ROOT = Path(__file__).parents[2]
DAY = 24 * 60  # minutes


def _load(transactions: list[Transaction]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE tx (transaction_id BIGINT, transacted_at TIMESTAMP, "
        "sender_account_key VARCHAR, receiver_account_key VARCHAR, amount_paid DECIMAL(20, 6), "
        "payment_currency VARCHAR, amount_paid_usd DOUBLE, amount_received DECIMAL(20, 6), "
        "receiving_currency VARCHAR, amount_received_usd DOUBLE, payment_format VARCHAR)"
    )
    con.executemany(
        f"INSERT INTO tx VALUES ({', '.join('?' * 11)})",
        [list(tx.model_dump().values()) for tx in transactions],
    )
    return con


def _query(con: duckdb.DuckDBPyConnection, source: str) -> dict[int, dict[str, object]]:
    rel = con.sql(tabular_features_sql(source))
    return {row[0]: dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()}


def _features(transactions: list[Transaction]) -> dict[int, dict[str, object]]:
    """Features of each transaction, computed from `transactions` alone."""
    with _load(transactions) as con:
        return _query(con, "tx")


def test_own_fields(make_tx: MakeTx) -> None:
    tx = make_tx(1, 75, sender="001:A", receiver="002:B", usd=1234.5).model_copy(
        update={"receiving_currency": "Euro", "payment_format": "wire"}
    )
    self_transfer = make_tx(2, 0, sender="001:A", receiver="001:A")

    f, g = _features([tx, self_transfer])[1], _features([self_transfer])[2]

    assert (f["amount_usd"], f["hour"], f["payment_format"]) == (1234.5, 10, "wire")
    assert (f["payment_currency"], f["receiving_currency"]) == ("US Dollar", "Euro")
    assert (f["is_cross_currency"], f["is_self_transfer"], f["same_bank"]) == (True, False, False)
    assert (g["is_cross_currency"], g["is_self_transfer"], g["same_bank"]) == (False, True, True)


def test_history_ignores_the_same_minute(make_tx: MakeTx) -> None:
    features = _features([make_tx(1, 0), make_tx(2, 0), make_tx(3, 0, sender="001:B")])

    assert {f["sender_out_count_1h"] for f in features.values()} == {0}
    assert {f["receiver_in_count_1h"] for f in features.values()} == {0}
    assert features[2]["pair_count_before"] == features[3]["reverse_pair_count_before"] == 0


@pytest.mark.parametrize(
    ("minutes", "in_1h", "in_24h"),
    [(60, 1, 1), (61, 0, 1), (DAY, 0, 1), (DAY + 1, 0, 0)],
)
def test_windows_include_their_start(
    make_tx: MakeTx, minutes: int, in_1h: int, in_24h: int
) -> None:
    f = _features([make_tx(1, 0), make_tx(2, minutes)])[2]

    assert (f["sender_out_count_1h"], f["sender_out_count_24h"]) == (in_1h, in_24h)


def test_account_history_in_both_directions(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, sender="001:A", receiver="001:B", usd=100),
        make_tx(2, 10, sender="001:A", receiver="001:B", usd=200),
        make_tx(3, 20, sender="001:A", receiver="001:C", usd=300),
        make_tx(4, 30, sender="001:D", receiver="001:A", usd=400),
        make_tx(5, 40, sender="001:C", receiver="001:B", usd=900),  # C received 3; B received 1, 2
    ]

    f = _features(txs)
    a, c = f[4], f[5]

    assert (a["receiver_out_count_24h"], a["receiver_out_amount_24h"]) == (3, 600)
    assert (a["receiver_out_counterparties_24h"], a["receiver_in_count_24h"]) == (2, 0)
    assert (c["sender_in_count_24h"], c["sender_in_amount_24h"]) == (1, 300)
    assert (c["sender_out_count_24h"], c["sender_minutes_since_previous"]) == (0, 20)
    assert (c["receiver_in_count_1h"], c["receiver_in_counterparties_24h"]) == (2, 1)
    assert c["receiver_in_amount_24h"] == 300
    assert f[1]["sender_minutes_since_previous"] is None


def test_pairs_and_the_reverse_direction(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, sender="001:A", receiver="001:B"),
        make_tx(2, 10, sender="001:A", receiver="001:B"),
        make_tx(3, 10, sender="001:B", receiver="001:A"),  # same minute as 2: does not see it
        make_tx(4, 20, sender="001:A", receiver="001:B"),
    ]

    f = _features(txs)

    assert [f[i]["pair_count_before"] for i in (1, 2, 3, 4)] == [0, 1, 0, 2]
    assert [f[i]["reverse_pair_count_before"] for i in (1, 2, 3, 4)] == [0, 0, 1, 1]


def test_amount_against_the_sender_mean(make_tx: MakeTx) -> None:
    txs = [make_tx(1, 0, usd=100), make_tx(2, 10, usd=300), make_tx(3, 20, usd=1000)]

    f = _features(txs)

    assert f[1]["amount_to_sender_mean_24h"] is None
    assert f[3]["amount_to_sender_mean_24h"] == 5.0


@pytest.mark.parametrize("seed", range(5))
def test_features_see_nothing_from_their_minute_or_later(make_tx: MakeTx, seed: int) -> None:
    """The leakage guard: removing every other transaction at or after t leaves the features of
    a transaction at t unchanged."""
    rng = random.Random(seed)
    accounts = [f"00{i % 2}:{i}" for i in range(4)]
    txs = [
        make_tx(
            i,
            rng.randrange(0, 2 * DAY, 60),  # about four transactions per minute used
            sender=rng.choice(accounts),
            receiver=rng.choice(accounts),
            usd=float(rng.randrange(1, 100) * 100),
        )
        for i in range(200)
    ]
    con = _load(txs)
    full = _query(con, "tx")

    for target in rng.sample(txs, 15):
        con.execute(
            "CREATE OR REPLACE VIEW past_and_target AS SELECT * FROM tx "
            f"WHERE transacted_at < '{target.transacted_at}' "
            f"OR transaction_id = {target.transaction_id}"
        )
        alone = _query(con, "past_and_target")
        assert alone[target.transaction_id] == full[target.transaction_id]


def test_build_writes_every_transaction_before_the_end_of_test(
    make_tx: MakeTx, fct_transactions_db: WriteDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    db = fct_transactions_db([make_tx(1, 0), make_tx(2, 10), make_tx(3, 2 * DAY)])
    base = Settings()
    settings = base.model_copy(
        update={
            "data_dir": tmp_path,
            "duckdb_path": db,
            "features_dir": tmp_path / "features",
            "splits": base.splits.model_copy(update={"test_end": datetime(2022, 9, 6)}),
        }
    )

    path = build_tabular_features(settings)

    rel = duckdb.sql(f"SELECT * FROM '{path}' ORDER BY transaction_id")
    assert tuple(rel.columns) == ("transaction_id", "transacted_at", *FEATURES)
    assert [row[0] for row in rel.fetchall()] == [1, 2]
