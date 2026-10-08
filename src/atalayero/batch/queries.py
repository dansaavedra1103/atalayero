"""Reading the serving database the batch publishes (ADR-0020): for the API and the dashboard.

Light on purpose: DuckDB and the shared schemas only, never the batch's own machinery. Each call
opens the serving database read-only for one query, so a new publication is never blocked and
the next call sees it. Queries take parameters, never interpolated values.
"""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from atalayero.schemas import (
    AlertPage,
    Case,
    CaseInvestigation,
    CaseReport,
    CaseTransaction,
    QueuedAlert,
    TransactionScore,
)
from atalayero.settings import Settings

# "<day>:<bank>:<account>", e.g. 2022-09-09:0026442:80AEBECB0 (ADR-0015).
ALERT_ID = re.compile(r"^\d{4}-\d{2}-\d{2}:\d{1,12}:[0-9A-F]{1,24}$")


class ServingUnavailableError(RuntimeError):
    """The batch has not published a serving database yet, or not with this table."""


def _connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    path = settings.batch.serving_path
    if not path.exists():
        raise ServingUnavailableError(f"{path} is missing: run `make replay` first")
    return duckdb.connect(str(path), read_only=True)


def _execute(con: duckdb.DuckDBPyConnection, query: str, params: dict[str, Any]) -> Any:  # noqa: ANN401
    try:
        return con.execute(query, params or None)
    except duckdb.CatalogException as error:
        raise ServingUnavailableError(
            f"the serving database has no such table ({error}): run `make replay` again"
        ) from error


def read_serving(settings: Settings, query: str, params: dict[str, Any]) -> pd.DataFrame:
    """One query on the serving database, as a DataFrame."""
    with _connect(settings) as con:
        return _execute(con, query, params).df()


def _rows(con: duckdb.DuckDBPyConnection, query: str, params: dict[str, Any]) -> list[dict]:
    cursor = _execute(con, query, params)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


_ALERT = """
    SELECT a.alert_id, a.account_key, a.day, a.sources, a.score, a.rank, a.transaction_ids,
        d.phase
    FROM alerts AS a JOIN days AS d USING (day)
"""


def _alert(row: dict) -> QueuedAlert:
    return QueuedAlert(
        **{
            **row,
            "sources": tuple(row["sources"]),
            "transaction_ids": tuple(row["transaction_ids"]),
        }
    )


def list_alerts(
    settings: Settings,
    day: date | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AlertPage:
    """The queue's alerts by day and rank, optionally of one day or one source (a rule ID, or
    "model")."""
    where = (
        "WHERE ($day IS NULL OR a.day = $day) "
        "AND ($source IS NULL OR list_contains(a.sources, $source))"
    )
    params = {"day": day, "source": source}
    with _connect(settings) as con:
        (total,) = _execute(con, f"SELECT count(*) FROM alerts AS a {where}", params).fetchone()
        rows = _rows(
            con,
            f"{_ALERT} {where} ORDER BY a.day, a.rank LIMIT $limit OFFSET $offset",
            {**params, "limit": limit, "offset": offset},
        )
    return AlertPage(total=total, limit=limit, offset=offset, alerts=tuple(_alert(r) for r in rows))


def investigation_path(settings: Settings, alert_id: str) -> Path:
    """Where the agent's investigation of a queued alert is kept: beside the alert's day
    (ADR-0024). The case sets' investigations, explained with other models, live apart."""
    day = alert_id.split(":", 1)[0]
    name = f"{alert_id.replace(':', '_')}.json"
    return settings.batch.dir / "days" / day / "investigations" / name


def _investigation(settings: Settings, alert_id: str) -> CaseInvestigation | None:
    """The agent's investigation of the alert, if it ran (`make investigate-day` or the
    `investigate_alerts` DAG)."""
    path = investigation_path(settings, alert_id)
    if not path.is_file():
        return None
    stored = json.loads(path.read_text())
    grounding = stored.get("grounding") or {}
    return CaseInvestigation(
        report=CaseReport(**stored["report"]) if stored.get("report") else None,
        grounded=grounding.get("grounded"),
        config_version=stored["config_version"],
        error=stored.get("error"),
        investigated_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
    )


def get_case(settings: Settings, alert_id: str) -> Case | None:
    """An alert of the queue with every transaction of its account-day; None if there is no
    such alert. The ID must look like one, before it goes anywhere near a file name."""
    if not ALERT_ID.match(alert_id):
        return None
    with _connect(settings) as con:
        alerts = _rows(con, f"{_ALERT} WHERE a.alert_id = $id", {"id": alert_id})
        if not alerts:
            return None
        transactions = _rows(
            con,
            """
            SELECT * EXCLUDE (alert_id) FROM alert_transactions
            WHERE alert_id = $id ORDER BY transacted_at, transaction_id
            """,
            {"id": alert_id},
        )
    return Case(
        alert=_alert(alerts[0]),
        transactions=tuple(CaseTransaction(**t) for t in transactions),
        investigation=_investigation(settings, alert_id),
    )


def get_score(settings: Settings, transaction_id: int) -> TransactionScore | None:
    """The score the batch gave a transaction, and the champion that gave it; None if no
    complete day scored it."""
    with _connect(settings) as con:
        rows = _rows(
            con,
            """
            SELECT s.transaction_id, s.day, d.phase, s.score, d.champion_family,
                d.champion_version,
                coalesce((
                    SELECT list(a.alert_id ORDER BY a.alert_id) FROM alerts AS a
                    WHERE a.day = s.day AND list_contains(a.transaction_ids, s.transaction_id)
                ), []) AS alert_ids
            FROM scores AS s JOIN days AS d USING (day)
            WHERE s.transaction_id = $id
            """,
            {"id": transaction_id},
        )
    if not rows:
        return None
    return TransactionScore(**{**rows[0], "alert_ids": tuple(rows[0]["alert_ids"])})
