"""Point-in-time tabular features (ADR-0008).

The features of a transaction at time t use its own fields and the history strictly before t:
transactions of the same minute, the transaction itself included, never count. History crosses
split boundaries backward, never forward, and stops at the end of the test split.
"""

import logging
from pathlib import Path

import duckdb

from atalayero.ingestion.source import sql_literal
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

CATEGORICAL = ("payment_format", "payment_currency", "receiving_currency")
OWN = ("amount_usd", "is_cross_currency", "is_self_transfer", "same_bank", "hour")

# Per account, over its legs (transactions sent or received) in a trailing window that ends just
# before t: name -> (aggregate, direction, window).
_ACCOUNT_HISTORY = {
    "out_count_1h": ("count(*)", "out", "last_1h"),
    "in_count_1h": ("count(*)", "in", "last_1h"),
    "out_count_24h": ("count(*)", "out", "last_24h"),
    "out_amount_24h": ("sum(amount_usd)", "out", "last_24h"),
    "out_counterparties_24h": ("count(DISTINCT counterparty)", "out", "last_24h"),
    "in_count_24h": ("count(*)", "in", "last_24h"),
    "in_amount_24h": ("sum(amount_usd)", "in", "last_24h"),
    "in_counterparties_24h": ("count(DISTINCT counterparty)", "in", "last_24h"),
}
_ACCOUNT = (*_ACCOUNT_HISTORY, "minutes_since_previous")
ACCOUNT = tuple(f"{side}_{name}" for side in ("sender", "receiver") for name in _ACCOUNT)
PAIR = ("pair_count_before", "reverse_pair_count_before", "amount_to_sender_mean_24h")

FEATURES = (*CATEGORICAL, *OWN, *ACCOUNT, *PAIR)

MEMORY_LIMIT = "4GB"


def tabular_features_sql(source: str) -> str:
    """A query for the features of every transaction in `source`, a table or view with the
    columns of `marts.fct_transactions`, computed from the transactions in `source` alone."""
    history = ",\n".join(
        f"coalesce({aggregate} FILTER (WHERE direction = '{direction}') OVER {window}, 0) AS {name}"
        for name, (aggregate, direction, window) in _ACCOUNT_HISTORY.items()
    )
    account = ",\n".join(
        f"{alias}.{name} AS {side}_{name}"
        for side, alias in (("sender", "s"), ("receiver", "r"))
        for name in _ACCOUNT
    )
    return f"""
    WITH legs AS (
        SELECT transaction_id, transacted_at, sender_account_key AS account_key,
            'out' AS direction, receiver_account_key AS counterparty, amount_paid_usd AS amount_usd
        FROM {source}
        UNION ALL
        SELECT transaction_id, transacted_at, receiver_account_key, 'in', sender_account_key,
            amount_received_usd
        FROM {source}
    ),
    account_history AS (
        SELECT
            transaction_id,
            direction,
            {history},
            date_diff('minute', max(transacted_at) OVER earlier, transacted_at)
                AS minutes_since_previous
        FROM legs
        WINDOW
            earlier AS (PARTITION BY account_key ORDER BY transacted_at
                RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING),
            last_1h AS (PARTITION BY account_key ORDER BY transacted_at
                RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING),
            last_24h AS (PARTITION BY account_key ORDER BY transacted_at
                RANGE BETWEEN INTERVAL 24 HOURS PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING)
    ),
    pairs AS (  -- earlier transactions from the same sender to the same receiver
        SELECT
            transaction_id,
            sender_account_key,
            receiver_account_key,
            transacted_at,
            count(*) OVER (PARTITION BY sender_account_key, receiver_account_key
                ORDER BY transacted_at
                RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING)
                AS count_before,
            count(*) OVER (PARTITION BY sender_account_key, receiver_account_key
                ORDER BY transacted_at
                RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS count_through
        FROM {source}
    )
    SELECT
        t.transaction_id,
        t.transacted_at,
        t.payment_format,
        t.payment_currency,
        t.receiving_currency,
        t.amount_paid_usd AS amount_usd,
        t.payment_currency <> t.receiving_currency AS is_cross_currency,
        t.sender_account_key = t.receiver_account_key AS is_self_transfer,
        split_part(t.sender_account_key, ':', 1) = split_part(t.receiver_account_key, ':', 1)
            AS same_bank,
        hour(t.transacted_at) AS hour,
        {account},
        p.count_before AS pair_count_before,
        coalesce(reverse.count_through, 0) AS reverse_pair_count_before,
        t.amount_paid_usd / (s.out_amount_24h / nullif(s.out_count_24h, 0))
            AS amount_to_sender_mean_24h
    FROM {source} AS t
    JOIN account_history AS s ON s.transaction_id = t.transaction_id AND s.direction = 'out'
    JOIN account_history AS r ON r.transaction_id = t.transaction_id AND r.direction = 'in'
    JOIN pairs AS p ON p.transaction_id = t.transaction_id
    ASOF LEFT JOIN pairs AS reverse  -- the latest earlier transaction the other way round
        ON reverse.sender_account_key = t.receiver_account_key
        AND reverse.receiver_account_key = t.sender_account_key
        AND t.transacted_at > reverse.transacted_at
    """


def build_tabular_features(settings: Settings) -> Path:
    """Features of every transaction before the end of the test split, without labels, to
    `<features_dir>/tabular.parquet`; returns the path."""
    path = settings.features_dir / "tabular.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    end = settings.splits.test_end.isoformat(sep=" ")
    with duckdb.connect() as con:
        con.execute("SET enable_progress_bar = false")
        con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")  # windows spill to disk beyond it
        con.execute(f"SET temp_directory = {sql_literal(str(settings.data_dir / 'tmp'))}")
        con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        con.execute(
            "CREATE TEMP VIEW transactions AS SELECT * FROM wh.marts.fct_transactions "
            f"WHERE transacted_at < TIMESTAMP {sql_literal(end)}"
        )
        con.execute(
            f"COPY ({tabular_features_sql('transactions')} ORDER BY t.transaction_id) "
            f"TO {sql_literal(str(path))} (FORMAT parquet)"
        )
        (rows,) = con.execute(f"SELECT count(*) FROM {sql_literal(str(path))}").fetchone()
    logger.info("Wrote %d features for %d transactions to %s", len(FEATURES), rows, path)
    return path
