"""The serving database (ADR-0020): what the API and the dashboard read, published by the batch.

DuckDB allows one writer or many readers of a file, not both. The batch and dbt write the
warehouse; the API and the dashboard only ever open `batch.serving_path`, read-only. The batch
builds a new serving database from the complete days and moves it into place in one step, so a
reader sees either the old one or the new one, and never blocks a run.

It holds no label of any transaction or alert: only what the monitoring system knows at alert
time, and the KPIs dbt builds from the batch (`dbt build --select tag:batch`). Those are
aggregates computed with the dataset's labels, in hindsight, and say so.
"""

import logging
import os
from pathlib import Path

import duckdb
import pandas as pd

from atalayero.batch.day import day_dir, manifests
from atalayero.ingestion.source import sql_literal
from atalayero.settings import Settings
from atalayero.streaming.consumer import ALERT_COLUMNS

logger = logging.getLogger(__name__)

KPI_TABLES = ("kpi_daily", "kpi_rules", "kpi_typologies")  # dbt marts over the batch's files

# Schemas of the tables built from day files, for when no day has written one yet.
_ALERTS = (
    "alert_id VARCHAR, day DATE, account_key VARCHAR, sources VARCHAR[], score DOUBLE, "
    "rank BIGINT, transaction_ids BIGINT[]"
)
_DRIFT = {  # the fields of `DayDrift`
    "day": "DATE",
    "compared_with": "VARCHAR",
    "features": "STRUCT(feature VARCHAR, psi DOUBLE)[]",
    "moderate": "BIGINT",
    "significant": "BIGINT",
    "rule_alerts": "BIGINT",
    "rule_alerts_reference": "DOUBLE",
    "drift_detected": "BOOLEAN",
    "reasons": "VARCHAR[]",
}


def _files(paths: list[Path]) -> str:
    return "[" + ", ".join(sql_literal(str(p)) for p in paths) + "]"


def _table(con: duckdb.DuckDBPyConnection, name: str, query: str | None, schema: str) -> None:
    if query is None:
        con.execute(f"CREATE TABLE {name} ({schema})")
    else:
        con.execute(f"CREATE TABLE {name} AS {query}")


def build_serving(settings: Settings, path: Path) -> dict[str, int]:
    """Write a serving database from the complete days to `path`; return its tables' rows."""
    days = manifests(settings)
    frame = pd.DataFrame(
        [
            {
                "day": m.day,
                "phase": m.phase,
                "transactions": m.transactions,
                "rule_alerts": m.rule_alerts,
                "alerts": m.alerts,
                "champion_family": m.champion.family if m.champion else None,
                "champion_version": m.champion.version if m.champion else None,
                "rules": ", ".join(f"{k} v{v}" for k, v in sorted(m.rules.items())),
                "drift_detected": m.drift_detected,
                "written_at": m.written_at,
            }
            for m in days
        ],
        columns=[
            "day",
            "phase",
            "transactions",
            "rule_alerts",
            "alerts",
            "champion_family",
            "champion_version",
            "rules",
            "drift_detected",
            "written_at",
        ],
    )
    queued = [day_dir(settings, m.day) for m in days if m.alerts is not None]
    drifted = [day_dir(settings, m.day) for m in days if m.drift_detected is not None]
    with duckdb.connect(str(path)) as con:
        con.execute("SET enable_progress_bar = false")
        con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        con.register("days_frame", frame)
        con.execute(
            "CREATE TABLE days AS SELECT * REPLACE (day::DATE AS day, alerts::BIGINT AS alerts, "
            "drift_detected::BOOLEAN AS drift_detected) FROM days_frame ORDER BY day"
        )
        con.unregister("days_frame")
        alerts = [d / "alerts.parquet" for d in queued]
        _table(
            con,
            "alerts",
            f"SELECT * FROM read_parquet({_files(alerts)}) ORDER BY day, rank" if alerts else None,
            _ALERTS,
        )
        rule_alerts = [day_dir(settings, m.day) / "rule_alerts.parquet" for m in days]
        _table(
            con,
            "rule_alerts",
            f"SELECT * FROM read_parquet({_files(rule_alerts)}) ORDER BY triggered_at, alert_id"
            if rule_alerts
            else None,
            ALERT_COLUMNS,
        )
        # Every transaction of each alerted account-day, sent or received, with its score.
        scores = [d / "scores.parquet" for d in queued]
        _table(
            con,
            "alert_transactions",
            f"""
            SELECT
                a.alert_id,
                t.transaction_id,
                t.transacted_at,
                CASE WHEN t.sender_account_key = a.account_key THEN 'out' ELSE 'in' END
                    AS direction,
                t.sender_account_key,
                t.receiver_account_key,
                t.amount_paid,
                t.payment_currency,
                t.amount_paid_usd,
                t.amount_received,
                t.receiving_currency,
                t.amount_received_usd,
                t.payment_format,
                s.score,
                list_contains(a.transaction_ids, t.transaction_id) AS raised_alert
            FROM alerts AS a
            JOIN wh.marts.fct_transactions AS t
                ON t.transacted_at::date = a.day
                AND a.account_key IN (t.sender_account_key, t.receiver_account_key)
            JOIN read_parquet({_files(scores)}) AS s USING (transaction_id)
            ORDER BY a.alert_id, t.transacted_at, t.transaction_id
            """
            if scores
            else None,
            "alert_id VARCHAR, transaction_id BIGINT, transacted_at TIMESTAMP, direction VARCHAR, "
            "sender_account_key VARCHAR, receiver_account_key VARCHAR, "
            "amount_paid DECIMAL(18,2), payment_currency VARCHAR, amount_paid_usd DOUBLE, "
            "amount_received DECIMAL(18,2), receiving_currency VARCHAR, "
            "amount_received_usd DOUBLE, payment_format VARCHAR, score DOUBLE, "
            "raised_alert BOOLEAN",
        )
        drift = [d / "drift.json" for d in drifted]
        drift_columns = "{" + ", ".join(f"'{n}': '{k}'" for n, k in _DRIFT.items()) + "}"
        _table(
            con,
            "drift",
            f"SELECT * FROM read_json({_files(drift)}, columns = {drift_columns}) ORDER BY day"
            if drift
            else None,
            ", ".join(f"{name} {kind}" for name, kind in _DRIFT.items()),
        )
        built = {
            name
            for (name,) in con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE database_name = 'wh' AND schema_name = 'marts'"
            ).fetchall()
        }
        for name in KPI_TABLES:
            if name in built:
                con.execute(f"CREATE TABLE {name} AS SELECT * FROM wh.marts.{name} ORDER BY ALL")
            else:
                logger.warning("No %s in the warehouse: run `dbt build --select tag:batch`", name)
        tables = ("days", "alerts", "rule_alerts", "alert_transactions", "drift")
        rows = {
            name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in (*tables, *(k for k in KPI_TABLES if k in built))
        }
        con.execute("DETACH wh")
    return rows


def publish(settings: Settings) -> Path:
    """Build the serving database next to its place and move it there in one step."""
    target = settings.batch.serving_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.unlink(missing_ok=True)
    rows = build_serving(settings, tmp)
    os.replace(tmp, target)
    logger.info("Published %s: %s", target, ", ".join(f"{name} {n}" for name, n in rows.items()))
    return target
