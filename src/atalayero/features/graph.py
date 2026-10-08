"""Point-in-time graph features from daily snapshots (ADR-0008).

The snapshot of day d is the graph of the transactions in `[d - 3 days, d)`, self-transfers left
out. Every transaction of day d takes the features of its sender and its receiver in that
snapshot, so nothing from day d or later reaches them. Accounts missing from a snapshot (no
transactions in those three days) get zeros.
"""

import logging
import multiprocessing
from collections.abc import Iterator, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import networkx as nx
import numpy as np

from atalayero.ingestion.source import sql_literal
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

SNAPSHOT_DAYS = 3
# PageRank and Louvain community sizes were dropped: they shift with each snapshot's size and
# make-up, which no stable feature should do (ADR-0013).
_ACCOUNT = (
    "in_degree",  # distinct senders to the account
    "out_degree",  # distinct receivers from the account
    "in_amount",  # US Dollar received
    "out_amount",  # US Dollar sent
    "in_short_cycle",  # on a directed cycle of two or three accounts
)
GRAPH = tuple(f"{side}_graph_{name}" for side in ("sender", "receiver") for name in _ACCOUNT)
_DTYPES = {
    "in_degree": int,
    "out_degree": int,
    "in_amount": float,
    "out_amount": float,
    "in_short_cycle": bool,
}

Edge = tuple[str, str, int, float]  # (sender, receiver, transactions, US Dollar)


def short_cycle_accounts(graph: nx.DiGraph) -> set[str]:
    """Accounts on a directed cycle of two or three distinct accounts."""
    found: set[str] = set()
    for u, v in graph.edges():
        if graph.has_edge(v, u):
            found.update((u, v))
            continue
        if u in found and v in found:
            continue
        after_v, before_u = graph._succ[v], graph._pred[u]
        small, large = (after_v, before_u) if len(after_v) <= len(before_u) else (before_u, after_v)
        for w in small:
            if w in large and w not in (u, v):
                found.update((u, v, w))
                break
    return found


def snapshot_features(edges: Sequence[Edge]) -> tuple[list[str], dict[str, np.ndarray]]:
    """Accounts of one snapshot and their features, in `_ACCOUNT` order, from its edges."""
    graph = nx.DiGraph()
    for sender, receiver, transactions, usd in edges:  # edges come sorted: a stable graph
        graph.add_edge(sender, receiver, count=transactions, usd=usd)
    accounts = list(graph.nodes)
    if not accounts:
        return [], {name: np.zeros(0, dtype=_DTYPES[name]) for name in _ACCOUNT}
    cycles = short_cycle_accounts(graph)
    values = {
        "in_degree": [graph.in_degree(a) for a in accounts],
        "out_degree": [graph.out_degree(a) for a in accounts],
        "in_amount": [graph.in_degree(a, weight="usd") for a in accounts],
        "out_amount": [graph.out_degree(a, weight="usd") for a in accounts],
        "in_short_cycle": [a in cycles for a in accounts],
    }
    return accounts, {name: np.array(v, dtype=_DTYPES[name]) for name, v in values.items()}


def _snapshots(
    con: duckdb.DuckDBPyConnection, source: str, days: Sequence[date] | None = None
) -> Iterator[tuple[date, list[Edge]]]:
    """For each day with transactions in `source`, or each of `days`, the edges of its
    snapshot."""
    if days is None:
        days = [
            d
            for (d,) in con.execute(
                f"SELECT DISTINCT transacted_at::date FROM {source} ORDER BY 1"
            ).fetchall()
        ]
    for day in days:
        end = datetime.combine(day, datetime.min.time())
        edges = con.execute(
            f"""
            SELECT sender_account_key, receiver_account_key, count(*), sum(amount_paid_usd)
            FROM {source}
            WHERE transacted_at >= $start AND transacted_at < $end
                AND sender_account_key <> receiver_account_key
            GROUP BY ALL
            ORDER BY ALL
            """,
            {"start": end - timedelta(days=SNAPSHOT_DAYS), "end": end},
        ).fetchall()
        yield day, edges


def graph_features(
    con: duckdb.DuckDBPyConnection,
    source: str,
    executor: Executor | None = None,
    days: Sequence[date] | None = None,
) -> duckdb.DuckDBPyRelation:
    """The graph features of every transaction in `source` (a table or view with the columns of
    `marts.fct_transactions`), from the transactions in `source` alone; only of the transactions
    of `days`, if given, so that only their snapshots are built. Snapshots run on `executor` if
    given."""
    snapshots = list(_snapshots(con, source, days))
    days = [day for day, _ in snapshots]
    if executor is None:
        results = [snapshot_features(edges) for _, edges in snapshots]
    else:
        results = list(executor.map(snapshot_features, [edges for _, edges in snapshots]))
    for day, (accounts, _) in zip(days, results, strict=True):
        logger.info("Snapshot for %s: %d accounts", day, len(accounts))

    columns: dict[str, np.ndarray] = {
        "account_key": np.array([a for accounts, _ in results for a in accounts], dtype=object),
        "day": np.array(
            [day for day, (accounts, _) in zip(days, results, strict=True) for _ in accounts],
            dtype="datetime64[us]",
        ),
    }
    for name in _ACCOUNT:
        columns[name] = np.concatenate([features[name] for _, features in results])
    con.register("graph_accounts", columns)
    where = ""
    if days is not None:
        listed = ", ".join(f"DATE '{day.isoformat()}'" for day in days)
        where = f"WHERE t.transacted_at::date IN ({listed})"
    selected = ",\n".join(
        f"coalesce({alias}.{name}, {'false' if name == 'in_short_cycle' else '0'}) "
        f"AS {side}_graph_{name}"
        for side, alias in (("sender", "s"), ("receiver", "r"))
        for name in _ACCOUNT
    )
    return con.sql(
        f"""
        SELECT t.transaction_id, {selected}
        FROM {source} AS t
        LEFT JOIN graph_accounts AS s
            ON s.account_key = t.sender_account_key AND s.day::date = t.transacted_at::date
        LEFT JOIN graph_accounts AS r
            ON r.account_key = t.receiver_account_key AND r.day::date = t.transacted_at::date
        {where}
        """
    )


def build_graph_features(settings: Settings) -> Path:
    """Graph features of every transaction before the end of the test split, to
    `<features_dir>/graph.parquet`; returns the path."""
    path = settings.features_dir / "graph.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    end = settings.splits.test_end.isoformat(sep=" ")
    # Spawned, not forked: the parent holds DuckDB threads that a fork would copy mid-flight.
    spawn = multiprocessing.get_context("spawn")
    with (
        duckdb.connect() as con,
        ProcessPoolExecutor(settings.graph_workers, mp_context=spawn) as executor,
    ):
        con.execute("SET enable_progress_bar = false")
        con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        con.execute(
            "CREATE TEMP VIEW transactions AS SELECT * FROM wh.marts.fct_transactions "
            f"WHERE transacted_at < TIMESTAMP {sql_literal(end)}"
        )
        graph_features(con, "transactions", executor).order("transaction_id").write_parquet(
            str(path)
        )
        (rows,) = con.execute(f"SELECT count(*) FROM {sql_literal(str(path))}").fetchone()
    logger.info("Wrote %d graph features for %d transactions to %s", len(GRAPH), rows, path)
    return path
