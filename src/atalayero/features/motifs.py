"""Point-in-time motif features (ADR-0011): the temporal patterns laundering typologies leave.

One pass in event-time order, a minute at a time: every transaction of a minute is scored against
the state built from earlier minutes, and only then does the whole minute join the state, so
nothing from a transaction's own minute or later reaches its features (ADR-0008).

- `cycle_length`, `cycle_hours`: the shortest chain in time order, inside the window, that leads
  from the receiver back to the sender with amounts close to this one, so that this transaction
  closes a cycle in which money comes back almost whole; its length in transactions (0 if none)
  and the hours from its first leg to this transaction.
- `relay_in_96h`: transactions the sender received in the window with an amount close to this
  one (it sends 0.85 to 1.2 times what it received): money passing through.
- `relay_depth`: how many such relays precede this one in a chain (layering).
- `fan_paths`: other accounts that got money from the sender's own senders and sent money to this
  receiver: the parallel paths of a scatter-gather.
"""

import logging
from collections import deque
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path

import duckdb
import numpy as np

from atalayero.ingestion.source import sql_literal
from atalayero.rules.online import RecentLegs
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=96)  # documented cycles and relays finish within it (ADR-0011)
MAX_CYCLE = 6  # transactions, the closing one included
CYCLE_BAND = 1.5  # every leg of a cycle within this factor of the closing amount
RELAY_BAND = (0.85, 1.2)  # amount sent over amount received
MOTIFS = ("cycle_length", "cycle_hours", "relay_in_96h", "relay_depth", "fan_paths")

Row = tuple[int, datetime, str, str, float, float]  # id, time, sender, receiver, paid, received


class MotifState:
    def __init__(self) -> None:
        self.legs = RecentLegs(WINDOW)
        # receiver -> (time, US Dollar received, relay depth of that transaction), by time
        self.received: dict[str, deque[tuple[datetime, float, int]]] = {}
        self._received_order: deque[tuple[datetime, str]] = deque()

    def expire(self, now: datetime) -> None:
        self.legs.expire(now)
        start = now - WINDOW
        while self._received_order and self._received_order[0][0] < start:
            _, account = self._received_order.popleft()
            entries = self.received[account]
            entries.popleft()
            if not entries:
                del self.received[account]

    def features(self, row: Row) -> tuple[int, float | None, int, int, int]:
        _, now, sender, receiver, paid, _ = row
        if sender == receiver:
            return 0, None, 0, 0, 0
        cycle_length, cycle_hours = 0, None
        chain = self.legs.find_chain(
            receiver, sender, now, MAX_CYCLE - 1, (paid / CYCLE_BAND, paid * CYCLE_BAND)
        )
        if chain is not None:
            cycle_length = len(chain) + 1
            first = self._time_of(receiver, chain[0])
            cycle_hours = (now - first).total_seconds() / 3600
        low, high = RELAY_BAND
        relays = [
            depth
            for _, amount, depth in self.received.get(sender, ())
            if amount > 0 and low <= paid / amount <= high
        ]
        relay_depth = 1 + max(relays) if relays else 0
        return (
            cycle_length,
            cycle_hours,
            len(relays),
            relay_depth,
            self._fan_paths(sender, receiver),
        )

    def _time_of(self, sender: str, transaction_id: int) -> datetime:
        """Time of a leg sent by `sender`, found in the index."""
        for legs in self.legs.out[sender].values():
            for time, leg_id, _ in legs:
                if leg_id == transaction_id:
                    return time
        raise KeyError(transaction_id)

    def _fan_paths(self, sender: str, receiver: str) -> int:
        """Distinct accounts M, other than both ends, that received from one of the sender's
        senders and sent to the receiver."""
        funders = self.legs.into.get(sender, {}).keys()
        into_receiver = self.legs.into.get(receiver, {})
        if not funders or not into_receiver:
            return 0
        candidates = into_receiver.keys() - {sender, receiver}
        # Walk from the cheaper side: the funders' receivers, or the receiver's senders.
        forward_cost = sum(len(self.legs.out.get(f, ())) for f in funders)
        if forward_cost <= sum(len(self.legs.into.get(m, ())) for m in candidates):
            reached = {m for f in funders for m in self.legs.out.get(f, ())}
            return len(candidates & reached)
        return sum(1 for m in candidates if not funders.isdisjoint(self.legs.into.get(m, {})))

    def add(self, row: Row, relay_depth: int) -> None:
        transaction_id, now, sender, receiver, paid, received = row
        self.legs.add(now, sender, receiver, transaction_id, paid)
        if sender == receiver:
            return
        self.received.setdefault(receiver, deque()).append((now, received, relay_depth))
        self._received_order.append((now, receiver))


def motif_features(
    rows: Iterable[Row], state: MotifState | None = None
) -> Iterator[tuple[int, int, float | None, int, int, int]]:
    """Motif features of each row, given in event-time order: (transaction_id, *MOTIFS).

    `state` carries on from where an earlier pass over the preceding rows stopped, and is updated
    in place (ADR-0020)."""
    state = MotifState() if state is None else state
    for now, minute in groupby(rows, key=lambda row: row[1]):
        batch = list(minute)
        state.expire(now)
        scored = [(row, state.features(row)) for row in batch]
        for row, features in scored:
            yield (row[0], *features)
        for row, features in scored:
            state.add(row, features[3])


def motif_features_frame(
    con: duckdb.DuckDBPyConnection, source: str, state: MotifState | None = None
) -> duckdb.DuckDBPyRelation:
    """The motif features of every transaction in `source` (a table or view with the columns of
    `marts.fct_transactions`), from the transactions in `source` alone, or carrying on from
    `state` (updated in place)."""
    cursor = con.execute(
        f"""
        SELECT transaction_id, transacted_at, sender_account_key, receiver_account_key,
            amount_paid_usd, amount_received_usd
        FROM {source}
        ORDER BY transacted_at, transaction_id
        """
    )

    def rows() -> Iterator[Row]:
        while batch := cursor.fetchmany(50_000):
            yield from batch

    results = list(motif_features(rows(), state))
    columns = list(zip(*results, strict=True)) if results else [[] for _ in range(6)]
    con.register(
        "motifs",
        {
            "transaction_id": np.array(columns[0], dtype=np.int64),
            "cycle_length": np.array(columns[1], dtype=np.int64),
            "cycle_hours": np.array(columns[2], dtype=float),  # None becomes NaN
            "relay_in_96h": np.array(columns[3], dtype=np.int64),
            "relay_depth": np.array(columns[4], dtype=np.int64),
            "fan_paths": np.array(columns[5], dtype=np.int64),
        },
    )
    return con.sql("SELECT * FROM motifs")


def build_motif_features(settings: Settings) -> Path:
    """Motif features of every transaction before the end of the test split, to
    `<features_dir>/motifs.parquet`; returns the path."""
    path = settings.features_dir / "motifs.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    end = settings.splits.test_end.isoformat(sep=" ")
    with duckdb.connect() as con:
        con.execute("SET enable_progress_bar = false")
        con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        con.execute(
            "CREATE TEMP VIEW transactions AS SELECT * FROM wh.marts.fct_transactions "
            f"WHERE transacted_at < TIMESTAMP {sql_literal(end)}"
        )
        motif_features_frame(con, "transactions").order("transaction_id").write_parquet(str(path))
        (rows,) = con.execute(f"SELECT count(*) FROM {sql_literal(str(path))}").fetchone()
    logger.info("Wrote %d motif features for %d transactions to %s", len(MOTIFS), rows, path)
    return path
