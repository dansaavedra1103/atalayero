import random
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from atalayero.features.motifs import (
    MAX_CYCLE,
    MOTIFS,
    WINDOW,
    build_motif_features,
    motif_features_frame,
)
from atalayero.schemas import Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
LoadTx = Callable[[list[Transaction]], duckdb.DuckDBPyConnection]
WriteDb = Callable[[list[Transaction]], Path]

REPO_ROOT = Path(__file__).parents[2]
HOUR = 60  # minutes
WINDOW_MINUTES = int(WINDOW.total_seconds() // 60)


def _rows(con: duckdb.DuckDBPyConnection, source: str) -> dict[int, dict[str, object]]:
    rel = motif_features_frame(con, source)
    return {row[0]: dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()}


@pytest.fixture
def motifs(load_tx: LoadTx) -> Callable[[list[Transaction]], dict[int, dict[str, object]]]:
    def compute(transactions: list[Transaction]) -> dict[int, dict[str, object]]:
        with load_tx(transactions) as con:
            return _rows(con, "tx")

    return compute


Motifs = Callable[[list[Transaction]], dict[int, dict[str, object]]]


def test_closing_a_cycle(make_tx: MakeTx, motifs: Motifs) -> None:
    f = motifs(
        [
            make_tx(1, 0, sender="001:A", receiver="001:B"),
            make_tx(2, 5 * HOUR, sender="001:B", receiver="001:C"),
            make_tx(3, 30 * HOUR, sender="001:C", receiver="001:A"),
        ]
    )

    assert (f[3]["cycle_length"], f[3]["cycle_hours"]) == (3, 30.0)
    assert (f[2]["cycle_length"], f[2]["cycle_hours"]) == (0, None)  # NaN is stored as NULL


@pytest.mark.parametrize(
    ("closing_minute", "length"),
    [(0, 0), (1, 2), (WINDOW_MINUTES, 2), (WINDOW_MINUTES + 1, 0)],
    ids=["same minute", "next minute", "window edge", "too late"],
)
def test_cycles_need_an_earlier_minute_inside_the_window(
    make_tx: MakeTx, motifs: Motifs, closing_minute: int, length: int
) -> None:
    f = motifs(
        [
            make_tx(1, 0, sender="001:A", receiver="001:B"),
            make_tx(2, closing_minute, sender="001:B", receiver="001:A"),
        ]
    )

    assert f[2]["cycle_length"] == length


def test_cycles_need_amounts_close_to_the_closing_one(make_tx: MakeTx, motifs: Motifs) -> None:
    def closing(first_leg_usd: float) -> int:
        return motifs(
            [
                make_tx(1, 0, sender="001:A", receiver="001:B", usd=first_leg_usd),
                make_tx(2, HOUR, sender="001:B", receiver="001:A", usd=1000),
            ]
        )[2]["cycle_length"]

    assert (closing(1490), closing(1510), closing(670), closing(660)) == (2, 0, 2, 0)


def test_cycles_longer_than_the_limit_do_not_count(make_tx: MakeTx, motifs: Motifs) -> None:
    accounts = [f"001:{i}" for i in range(MAX_CYCLE + 1)] + ["001:0"]
    txs = [
        make_tx(i, i * HOUR, sender=s, receiver=r)
        for i, (s, r) in enumerate(zip(accounts, accounts[1:], strict=False))
    ]

    assert motifs(txs)[len(txs) - 1]["cycle_length"] == 0


def test_relays_pass_money_on_with_a_similar_amount(make_tx: MakeTx, motifs: Motifs) -> None:
    f = motifs(
        [
            make_tx(1, 0, sender="001:A", receiver="001:B", usd=1000),
            make_tx(2, 10 * HOUR, sender="001:B", receiver="001:C", usd=950),  # relay
            make_tx(3, 20 * HOUR, sender="001:C", receiver="001:D", usd=940),  # relay of a relay
            make_tx(4, 21 * HOUR, sender="001:C", receiver="001:E", usd=300),  # not similar
            make_tx(5, 22 * HOUR, sender="001:B", receiver="001:F", usd=1150),  # relay of 1
        ]
    )

    assert [(f[i]["relay_in_96h"], f[i]["relay_depth"]) for i in range(1, 6)] == [
        (0, 0),
        (1, 1),
        (1, 2),
        (0, 0),
        (1, 1),
    ]


def test_fan_paths_count_parallel_intermediaries(make_tx: MakeTx, motifs: Motifs) -> None:
    txs = [make_tx(i, i, sender="001:A", receiver=f"001:B{i}") for i in range(1, 4)]
    txs += [
        make_tx(4, HOUR, sender="001:B1", receiver="001:C"),
        make_tx(5, HOUR + 1, sender="001:B2", receiver="001:C"),
        make_tx(6, 2 * HOUR, sender="001:X", receiver="001:C"),  # not funded by A
        make_tx(7, 3 * HOUR, sender="001:B3", receiver="001:C"),
    ]

    f = motifs(txs)

    assert [f[i]["fan_paths"] for i in (4, 5, 7)] == [0, 1, 2]


def test_self_transfers_have_no_motifs(make_tx: MakeTx, motifs: Motifs) -> None:
    f = motifs([make_tx(1, 0, receiver="001:A"), make_tx(2, 10, sender="001:A", receiver="001:A")])

    assert (f[2]["cycle_length"], f[2]["relay_in_96h"], f[2]["fan_paths"]) == (0, 0, 0)


@pytest.mark.parametrize("seed", range(3))
def test_motifs_see_nothing_from_their_minute_or_later(
    make_tx: MakeTx, load_tx: LoadTx, seed: int
) -> None:
    """The leakage guard of ADR-0008."""
    rng = random.Random(seed)
    accounts = [f"00{i % 2}:{i}" for i in range(5)]
    txs = [
        make_tx(
            i,
            rng.randrange(0, 5 * 24 * HOUR, 60),
            sender=rng.choice(accounts),
            receiver=rng.choice(accounts),
            usd=float(rng.choice([1000, 1050, 1100, 3000])),
        )
        for i in range(200)
    ]
    con = load_tx(txs)
    full = _rows(con, "tx")

    for target in rng.sample(txs, 15):
        con.execute(
            "CREATE OR REPLACE VIEW past_and_target AS SELECT * FROM tx "
            f"WHERE transacted_at < '{target.transacted_at}' "
            f"OR transaction_id = {target.transaction_id}"
        )
        alone = _rows(con, "past_and_target")
        assert alone[target.transaction_id] == full[target.transaction_id]


def test_build_writes_every_transaction_before_the_end_of_test(
    make_tx: MakeTx, fct_transactions_db: WriteDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    db = fct_transactions_db([make_tx(1, 0), make_tx(2, 10), make_tx(3, 2 * 24 * HOUR)])
    base = Settings()
    settings = base.model_copy(
        update={
            "duckdb_path": db,
            "features_dir": tmp_path / "features",
            "splits": base.splits.model_copy(update={"test_end": datetime(2022, 9, 6)}),
        }
    )

    path = build_motif_features(settings)

    rel = duckdb.sql(f"SELECT * FROM '{path}' ORDER BY transaction_id")
    assert tuple(rel.columns) == ("transaction_id", *MOTIFS)
    assert [row[0] for row in rel.fetchall()] == [1, 2]
