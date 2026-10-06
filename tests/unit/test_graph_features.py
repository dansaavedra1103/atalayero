import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import duckdb
import networkx as nx
import pytest

from atalayero.features.graph import (
    GRAPH,
    build_graph_features,
    graph_features,
    short_cycle_accounts,
    snapshot_features,
)
from atalayero.schemas import Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
LoadTx = Callable[[list[Transaction]], duckdb.DuckDBPyConnection]
WriteDb = Callable[[list[Transaction]], Path]

REPO_ROOT = Path(__file__).parents[2]
DAY = 24 * 60  # minutes; T0 is 2022-09-05 09:00, so day k starts at k * DAY - 540


def _start_of_day(k: int) -> int:
    """Minutes after T0 at which day T0 + k starts."""
    return k * DAY - 9 * 60


def _rows(rel: duckdb.DuckDBPyRelation) -> dict[int, dict[str, object]]:
    return {row[0]: dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()}


def test_short_cycles_have_two_or_three_accounts() -> None:
    graph = nx.DiGraph(
        [("A", "B"), ("B", "A"), ("C", "D"), ("D", "E"), ("E", "C"), ("F", "G"), ("G", "H")]
        + [("H", "I"), ("I", "F")]  # four accounts: too long
    )

    assert short_cycle_accounts(graph) == {"A", "B", "C", "D", "E"}


def test_snapshot_features() -> None:
    edges = [("A", "B", 2, 100.0), ("A", "C", 1, 50.0), ("B", "C", 1, 10.0), ("C", "A", 1, 5.0)]
    edges += [("X", "Y", 1, 1.0)]  # another component

    accounts, f = snapshot_features(edges)
    a = accounts.index("A")

    assert sorted(accounts) == ["A", "B", "C", "X", "Y"]
    assert (f["out_degree"][a], f["in_degree"][a]) == (2, 1)
    assert (f["out_amount"][a], f["in_amount"][a]) == (150.0, 5.0)
    assert [bool(x) for x in f["in_short_cycle"]] == [a in "ABC" for a in accounts]


def test_empty_snapshot() -> None:
    accounts, f = snapshot_features([])

    assert accounts == []
    assert {name: len(v) for name, v in f.items()} == dict.fromkeys(f, 0)


def test_a_day_sees_the_three_days_before_it(make_tx: MakeTx, load_tx: LoadTx) -> None:
    txs = [
        make_tx(1, _start_of_day(-3) - 1, sender="001:A", receiver="001:Z"),  # 4 days before
        make_tx(2, _start_of_day(-3), sender="001:A", receiver="001:B"),  # first minute in
        make_tx(3, _start_of_day(-1), sender="001:A", receiver="001:C"),
        make_tx(4, 0, sender="001:A", receiver="001:D"),  # same day: not in the snapshot
        make_tx(5, 60, sender="001:A", receiver="001:E"),
        make_tx(6, 70, sender="001:Q", receiver="001:R"),  # accounts with no history
    ]

    with load_tx(txs) as con:
        f = _rows(graph_features(con, "tx"))

    assert (f[5]["sender_graph_out_degree"], f[5]["sender_graph_out_amount"]) == (2, 12000.0)
    assert f[1]["sender_graph_out_degree"] == 0  # nothing before it
    assert not any(f[6][name] for name in GRAPH)  # zeros and False


@pytest.mark.parametrize("seed", range(3))
def test_features_see_nothing_from_their_day_or_later(
    make_tx: MakeTx, load_tx: LoadTx, seed: int
) -> None:
    """The leakage guard of ADR-0008, as for the tabular features."""
    rng = random.Random(seed)
    accounts = [f"00{i % 2}:{i}" for i in range(6)]
    txs = [
        make_tx(
            i,
            rng.randrange(0, 5 * DAY, 60),
            sender=rng.choice(accounts),
            receiver=rng.choice(accounts),
            usd=float(rng.randrange(1, 100) * 100),
        )
        for i in range(150)
    ]
    con = load_tx(txs)
    full = _rows(graph_features(con, "tx"))

    for target in rng.sample(txs, 10):
        con.execute(
            "CREATE OR REPLACE VIEW past_and_target AS SELECT * FROM tx "
            f"WHERE transacted_at < '{target.transacted_at}' "
            f"OR transaction_id = {target.transaction_id}"
        )
        alone = _rows(graph_features(con, "past_and_target"))
        assert alone[target.transaction_id] == full[target.transaction_id]


def test_an_executor_gives_the_same_features(make_tx: MakeTx, load_tx: LoadTx) -> None:
    txs = [make_tx(i, i * 300, receiver=f"001:{i % 4}") for i in range(30)]
    con = load_tx(txs)

    with ThreadPoolExecutor(2) as executor:
        parallel = _rows(graph_features(con, "tx", executor))

    assert parallel == _rows(graph_features(con, "tx"))


def test_build_writes_every_transaction_before_the_end_of_test(
    make_tx: MakeTx, fct_transactions_db: WriteDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    db = fct_transactions_db([make_tx(1, 0), make_tx(2, DAY), make_tx(3, 2 * DAY)])
    base = Settings()
    settings = base.model_copy(
        update={
            "duckdb_path": db,
            "features_dir": tmp_path / "features",
            "graph_workers": 1,
            "splits": base.splits.model_copy(update={"test_end": datetime(2022, 9, 7)}),
        }
    )

    path = build_graph_features(settings)

    rel = duckdb.sql(f"SELECT * FROM '{path}' ORDER BY transaction_id")
    rows = rel.fetchall()
    assert tuple(rel.columns) == ("transaction_id", *GRAPH)
    assert [row[0] for row in rows] == [1, 2]
    assert dict(zip(rel.columns, rows[1], strict=True))["sender_graph_out_degree"] == 1
