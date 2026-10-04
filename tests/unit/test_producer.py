from collections.abc import Callable
from pathlib import Path

from atalayero.schemas import Transaction
from atalayero.streaming.producer import Pacer, iter_transactions

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]


def test_transactions_come_in_event_time_order(
    make_tx: MakeTx, fct_transactions_db: WriteDb
) -> None:
    # Stored out of order, with a same-minute tie that transaction_id breaks.
    db = fct_transactions_db([make_tx(3, 5), make_tx(1, 10), make_tx(2, 5), make_tx(4, 0)])

    assert [tx.transaction_id for tx in iter_transactions(db)] == [4, 2, 3, 1]
    assert [tx.transaction_id for tx in iter_transactions(db, limit=2)] == [4, 2]
    assert next(iter_transactions(db)) == make_tx(4, 0)


def _fake_time() -> tuple[Callable[[], float], Callable[[float], None], list[float]]:
    now, sleeps = [0.0], []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    return lambda: now[0], sleep, sleeps


def test_pacer_runs_event_time_at_the_given_speedup(make_tx: MakeTx) -> None:
    clock, sleep, sleeps = _fake_time()
    pacer = Pacer(60, clock, sleep)  # one simulated minute per real second

    for minutes in (0, 1, 1, 3):
        pacer.wait(make_tx(1, minutes).transacted_at)

    assert sleeps == [1.0, 2.0]


def test_pacer_without_speedup_never_sleeps(make_tx: MakeTx) -> None:
    clock, sleep, sleeps = _fake_time()
    pacer = Pacer(0, clock, sleep)

    for minutes in (0, 60, 120):
        pacer.wait(make_tx(1, minutes).transacted_at)

    assert sleeps == []
