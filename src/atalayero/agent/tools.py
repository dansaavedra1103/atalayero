"""The investigator's tools (ADR-0016): read-only views of the warehouse, cut at the end of the
alert's day, without labels.

- **Cut-off.** Every tool takes an alert ID, and the cut-off comes from the alert, not from the
  caller: an alert is reviewed once its day is over (ADR-0007), so an investigation sees nothing
  from later days.
- **No labels.** They live in their own mart, in the patterns file and in the case answers, and
  nothing here reads them.
- **Capped outputs.** A busy account cannot flood the investigator's context.
- **The model behind the score.** On the case sets, the models of the evaluation (ADR-0014); on
  the batch's queue, the champion that scored the alert's day, with the day's own features
  (ADR-0024).
"""

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any

import duckdb
import numpy as np
from sklearn.pipeline import Pipeline

from atalayero.agent.knowledge import Knowledge
from atalayero.ingestion.source import sql_literal
from atalayero.schemas import CaseAlert
from atalayero.settings import Settings, Split

MAX_TRANSACTIONS = 50  # rows `transactions` lists
MAX_NEIGHBOURS = 15  # counterparties `neighbourhood` lists, per hop
MAX_CYCLES = 10
MAX_DAYS = 7  # the longest look-back a tool takes
TOP_SCORED = 3  # transactions `explain_score` explains
TOP_FACTORS = 6  # features per explained transaction

# Each leg of an account: what it sent (out) and what it received (in), in USD as paid.
_LEGS = """
    SELECT transaction_id, transacted_at, 'out' AS direction,
        receiver_account_key AS counterparty, amount_paid_usd AS amount_usd,
        amount_paid AS amount, payment_currency AS currency, payment_format, is_self_transfer
    FROM wh.marts.fct_transactions
    WHERE sender_account_key = $account AND transacted_at >= $start AND transacted_at < $cutoff
    UNION ALL
    SELECT transaction_id, transacted_at, 'in', sender_account_key, amount_paid_usd,
        amount_received, receiving_currency, payment_format, is_self_transfer
    FROM wh.marts.fct_transactions
    WHERE receiver_account_key = $account AND transacted_at >= $start AND transacted_at < $cutoff
"""


def _usd(value: float | None) -> float:
    return round(float(value or 0.0), 2)


def _value(value: object) -> str | float | None:
    """A feature value as JSON: text as is, numbers rounded, missing as None."""
    if isinstance(value, str):
        return value
    number = float(value)
    return None if np.isnan(number) else round(number, 4)


class ToolBox:
    """The tools of one investigation run, over the alerts it may be asked about."""

    def __init__(
        self,
        settings: Settings,
        alerts: Iterable[CaseAlert],
        knowledge: Knowledge | None = None,
        batch: bool = False,
    ) -> None:
        self.settings = settings
        self.batch = batch  # the alerts are of the batch's queue, not of the case sets
        self._knowledge = knowledge  # loaded on first search
        self.alerts = {alert.alert_id: alert for alert in alerts}
        self.con = duckdb.connect()
        self.con.execute("SET enable_progress_bar = false")
        self.con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        self._models: dict[Split, Pipeline] = {}
        self._batch_models: dict[str, Pipeline] = {}  # by registered version

    def alert(self, alert_id: str) -> CaseAlert:
        try:
            return self.alerts[alert_id]
        except KeyError:
            raise ValueError(f"unknown alert {alert_id!r}") from None

    @staticmethod
    def cutoff(alert: CaseAlert) -> datetime:
        """The end of the alert's day: nothing at or after it is visible."""
        return datetime.combine(alert.day + timedelta(days=1), time())

    def _window(self, alert: CaseAlert, days: int | None) -> tuple[datetime, datetime]:
        cutoff = self.cutoff(alert)
        start = (
            datetime.min if days is None else cutoff - timedelta(days=min(max(days, 1), MAX_DAYS))
        )
        return start, cutoff

    def account_profile(self, alert_id: str, account_key: str | None = None) -> dict[str, Any]:
        """An account's activity over all history before the cut-off, and on the alert's day."""
        alert = self.alert(alert_id)
        account = account_key or alert.account_key
        start, cutoff = self._window(alert, None)
        day_start = cutoff - timedelta(days=1)
        params = {"account": account, "start": start, "cutoff": cutoff}
        rows = self.con.execute(
            f"""
            WITH legs AS ({_LEGS})
            SELECT
                direction,
                count(*), sum(amount_usd), count(DISTINCT counterparty),
                count(*) FILTER (WHERE transacted_at >= $day),
                sum(amount_usd) FILTER (WHERE transacted_at >= $day),
                count(DISTINCT counterparty) FILTER (WHERE transacted_at >= $day)
            FROM legs GROUP BY direction
            """,
            {**params, "day": day_start},
        ).fetchall()
        activity: dict[str, dict[str, dict[str, Any]]] = {"history": {}, "alert_day": {}}
        for direction in ("out", "in"):
            row = next((r for r in rows if r[0] == direction), (direction, 0, 0, 0, 0, 0, 0))
            key = "sent" if direction == "out" else "received"
            activity["history"][key] = {
                "count": row[1],
                "usd": _usd(row[2]),
                "counterparties": row[3],
            }
            activity["alert_day"][key] = {
                "count": row[4],
                "usd": _usd(row[5]),
                "counterparties": row[6],
            }
        first, last, self_transfers = self.con.execute(
            f"WITH legs AS ({_LEGS}) SELECT min(transacted_at), max(transacted_at), "
            "count(DISTINCT transaction_id) FILTER (WHERE is_self_transfer) FROM legs",
            params,
        ).fetchone()
        mix = {
            column: dict(
                self.con.execute(
                    f"WITH legs AS ({_LEGS}) SELECT {column}, count(*) FROM legs "
                    "GROUP BY ALL ORDER BY 2 DESC, 1",
                    params,
                ).fetchall()
            )
            for column in ("payment_format", "currency")
        }
        return {
            "account_key": account,
            "bank": account.split(":")[0],
            "visible_until": cutoff.isoformat(),
            "first_seen": first.isoformat() if first else None,
            "last_seen": last.isoformat() if last else None,
            **activity,
            "self_transfers": self_transfers,
            "payment_formats": mix["payment_format"],
            "currencies": mix["currency"],
        }

    def transactions(
        self, alert_id: str, account_key: str | None = None, days: int = 1, limit: int = 50
    ) -> dict[str, Any]:
        """An account's transactions in the last `days` before the cut-off, newest first, up to
        `limit` (at most MAX_TRANSACTIONS); totals cover all of them."""
        alert = self.alert(alert_id)
        account = account_key or alert.account_key
        start, cutoff = self._window(alert, days)
        params = {"account": account, "start": start, "cutoff": cutoff}
        totals = dict(
            (direction, (count, _usd(usd)))
            for direction, count, usd in self.con.execute(
                f"WITH legs AS ({_LEGS}) SELECT direction, count(*), sum(amount_usd) "
                "FROM legs GROUP BY direction",
                params,
            ).fetchall()
        )
        shown = min(max(limit, 1), MAX_TRANSACTIONS)
        rows = self.con.execute(
            f"""
            WITH legs AS ({_LEGS})
            SELECT transaction_id, transacted_at, direction, counterparty, amount_usd, amount,
                currency, payment_format
            FROM legs ORDER BY transacted_at DESC, transaction_id DESC LIMIT $shown
            """,
            {**params, "shown": shown},
        ).fetchall()
        return {
            "account_key": account,
            "window_start": start.isoformat(),
            "visible_until": cutoff.isoformat(),
            "sent": {
                "count": totals.get("out", (0, 0.0))[0],
                "usd": totals.get("out", (0, 0.0))[1],
            },
            "received": {
                "count": totals.get("in", (0, 0.0))[0],
                "usd": totals.get("in", (0, 0.0))[1],
            },
            "shown": len(rows),
            "transactions": [
                {
                    "transaction_id": tid,
                    "transacted_at": at.isoformat(),
                    "direction": direction,
                    "counterparty": counterparty,
                    "amount_usd": _usd(usd),
                    "amount": float(amount),
                    "currency": currency,
                    "payment_format": payment_format,
                }
                for tid, at, direction, counterparty, usd, amount, currency, payment_format in rows
            ],
        }

    def neighbourhood(
        self, alert_id: str, account_key: str | None = None, hops: int = 1, days: int = 3
    ) -> dict[str, Any]:
        """An account's counterparties in the last `days`, with the money each way and how many
        accounts each of them pays and is paid by in that time; with `hops=2`, the accounts its
        counterparties deal with too. Lists cycles that bring money back to the account in time
        order: A→B→A and A→B→C→A."""
        alert = self.alert(alert_id)
        account = account_key or alert.account_key
        start, cutoff = self._window(alert, days)
        params = {"account": account, "start": start, "cutoff": cutoff}
        edges = (
            "SELECT transaction_id AS id, sender_account_key AS s, receiver_account_key AS r, "
            "transacted_at AS t, amount_paid_usd AS usd FROM wh.marts.fct_transactions "
            "WHERE transacted_at >= $start AND transacted_at < $cutoff AND NOT is_self_transfer"
        )
        (total,) = self.con.execute(
            f"WITH legs AS ({_LEGS}) SELECT count(DISTINCT counterparty) FROM legs "
            "WHERE NOT is_self_transfer",
            params,
        ).fetchone()
        # Each counterparty's own reach in the window: a pattern around a hub shows at the hub,
        # so an account that is one leaf of it sees one ordinary payment.
        neighbours = self.con.execute(
            f"""
            WITH legs AS ({_LEGS}),
            top AS (
                SELECT counterparty,
                    count(*) FILTER (WHERE direction = 'out') AS sent,
                    sum(amount_usd) FILTER (WHERE direction = 'out') AS sent_usd,
                    count(*) FILTER (WHERE direction = 'in') AS received,
                    sum(amount_usd) FILTER (WHERE direction = 'in') AS received_usd
                FROM legs WHERE NOT is_self_transfer
                GROUP BY counterparty ORDER BY sum(amount_usd) DESC, counterparty LIMIT $n
            ),
            e AS ({edges}),
            pays AS (
                SELECT s AS k, count(DISTINCT r) AS n FROM e
                WHERE s IN (SELECT counterparty FROM top) GROUP BY s
            ),
            paid_by AS (
                SELECT r AS k, count(DISTINCT s) AS n FROM e
                WHERE r IN (SELECT counterparty FROM top) GROUP BY r
            )
            SELECT counterparty, sent, sent_usd, received, received_usd,
                coalesce(pays.n, 0), coalesce(paid_by.n, 0)
            FROM top
            LEFT JOIN pays ON pays.k = counterparty
            LEFT JOIN paid_by ON paid_by.k = counterparty
            ORDER BY coalesce(sent_usd, 0) + coalesce(received_usd, 0) DESC, counterparty
            """,
            {**params, "n": MAX_NEIGHBOURS},
        ).fetchall()
        cycles = self.con.execute(
            f"""
            WITH e AS ({edges}),
            outs AS (SELECT * FROM e WHERE s = $account),
            ins AS (SELECT * FROM e WHERE r = $account),
            found AS (
                SELECT [o.s, o.r, i.r] AS path, [o.id, i.id] AS ids, [o.usd, i.usd] AS usd
                FROM outs AS o JOIN ins AS i ON i.s = o.r AND i.t > o.t
                UNION ALL
                SELECT [o.s, o.r, m.r, i.r], [o.id, m.id, i.id], [o.usd, m.usd, i.usd]
                FROM outs AS o
                JOIN e AS m ON m.s = o.r AND m.t > o.t AND m.r <> $account
                JOIN ins AS i ON i.s = m.r AND i.t > m.t
            )
            SELECT path, ids, usd FROM found ORDER BY len(path), ids LIMIT $n
            """,
            {**params, "n": MAX_CYCLES},
        ).fetchall()
        result: dict[str, Any] = {
            "account_key": account,
            "window_start": start.isoformat(),
            "visible_until": cutoff.isoformat(),
            "counterparties": total,
            "top_counterparties": [
                {
                    "account_key": key,
                    "sent_to": {"count": sent, "usd": _usd(sent_usd)},
                    "received_from": {"count": received, "usd": _usd(received_usd)},
                    "accounts_it_pays": pays,
                    "accounts_paying_it": paid_by,
                }
                for key, sent, sent_usd, received, received_usd, pays, paid_by in neighbours
            ],
            "cycles": [
                {"path": path, "transaction_ids": ids, "usd": [_usd(u) for u in usd]}
                for path, ids, usd in cycles
            ],
        }
        if hops >= 2:
            second = self.con.execute(
                f"""
                WITH e AS ({edges}),
                direct AS (
                    SELECT DISTINCT CASE WHEN s = $account THEN r ELSE s END AS n
                    FROM e WHERE s = $account OR r = $account
                )
                SELECT CASE WHEN e.s = f.n THEN e.r ELSE e.s END AS account_key,
                    count(DISTINCT f.n), count(*), sum(e.usd)
                FROM e JOIN direct AS f ON e.s = f.n OR e.r = f.n
                WHERE e.s <> $account AND e.r <> $account
                GROUP BY ALL ORDER BY 4 DESC, 1 LIMIT $n
                """,
                {**params, "n": MAX_NEIGHBOURS},
            ).fetchall()
            result["second_hop"] = [
                {"account_key": key, "via": via, "transactions": n, "usd": _usd(usd)}
                for key, via, n, usd in second
            ]
        return result

    def _split(self, alert: CaseAlert) -> Split:
        day = datetime.combine(alert.day, time())
        for split in ("validation", "test"):
            start, end = self.settings.splits.bounds(split)
            if start <= day < end:
                return split
        raise ValueError(f"alert {alert.alert_id} is on no evaluated split")

    def _model(self, split: Split) -> Pipeline:
        """The model behind the alert's score: the champion on validation, trained on train
        alone; on test, the champion's family refit for the test run (ADR-0014)."""
        if split not in self._models:
            from atalayero.models.registry import Registry  # MLflow is slow to import

            registry = Registry(self.settings)
            self._models[split] = (
                registry.load_champion()
                if split == "validation"
                else registry.load_holdout_model(registry.champion_family())
            )
        return self._models[split]

    def _batch_model(self, day: date) -> Pipeline:
        """The champion that scored the batch's day, as the day's manifest records it."""
        from atalayero.batch.day import load_manifest
        from atalayero.models.registry import Registry  # MLflow is slow to import

        manifest = load_manifest(self.settings, day)
        if manifest is None or manifest.champion is None:
            raise ValueError(f"the batch has not scored {day}")
        version = manifest.champion.version
        if version not in self._batch_models:
            self._batch_models[version] = Registry(self.settings).load_version(version)
        return self._batch_models[version]

    def explain_score(self, alert_id: str) -> dict[str, Any]:
        """Why the model scored the alert's account-day as it did: its top-scored transactions,
        and the features that pushed each score up or down the most (LightGBM's own SHAP
        values, in log-odds)."""
        alert = self.alert(alert_id)
        start, cutoff = self._window(alert, 1)
        ids = [
            tid
            for (tid,) in self.con.execute(
                f"WITH legs AS ({_LEGS}) SELECT DISTINCT transaction_id FROM legs ORDER BY 1",
                {"start": start, "cutoff": cutoff, "account": alert.account_key},
            ).fetchall()
        ]
        model = self._batch_model(alert.day) if self.batch else self._model(self._split(alert))
        if not hasattr(model[-1], "booster_"):
            raise ValueError(
                f"explain_score needs a LightGBM model, not {type(model[-1]).__name__}"
            )
        if self.batch:
            from atalayero.batch.day import day_features

            features = day_features(self.settings, alert.day, ids)
        else:
            from atalayero.models.data import load_features

            features = load_features(self.settings, ids)
        scores = model.predict_proba(features)[:, 1]
        top = np.argsort(-scores, kind="stable")[:TOP_SCORED]
        inputs = model[:-1].transform(features.iloc[top])
        contributions = model[-1].predict(inputs, pred_contrib=True)
        explained = []
        for row, position in enumerate(top):
            values = features.iloc[position]
            order = np.argsort(-np.abs(contributions[row, :-1]), kind="stable")[:TOP_FACTORS]
            explained.append(
                {
                    "transaction_id": int(features.index[position]),
                    "score": round(float(scores[position]), 6),
                    "baseline_log_odds": round(float(contributions[row, -1]), 4),
                    "top_factors": [
                        {
                            "feature": features.columns[i],
                            "value": _value(values.iloc[i]),
                            "log_odds": round(float(contributions[row, i]), 4),
                        }
                        for i in order
                    ],
                }
            )
        return {
            "alert_id": alert.alert_id,
            "account_key": alert.account_key,
            "account_day_score": round(alert.score, 6),
            "rank_that_day": alert.rank,
            "transactions_that_day": len(ids),
            "explained": explained,
        }

    def lookup(self, transaction_ids: Iterable[int]) -> dict[int, tuple[datetime, float]]:
        """When each existing transaction happened and its USD amount as paid. Not a tool: the
        grounding check uses it to verify what a report cites, past the cut-off too."""
        rows = self.con.execute(
            "SELECT transaction_id, transacted_at, amount_paid_usd FROM wh.marts.fct_transactions "
            "WHERE list_contains($ids, transaction_id)",
            {"ids": [int(i) for i in transaction_ids]},
        ).fetchall()
        return {tid: (at, float(usd)) for tid, at, usd in rows}

    def search_typologies(self, query: str, k: int | None = None) -> dict[str, Any]:
        """The sections of the typology notes closest in meaning to `query`."""
        if self._knowledge is None:
            try:
                self._knowledge = Knowledge(self.settings)
            except FileNotFoundError as error:
                raise ValueError(str(error)) from error
        k = self.settings.knowledge.top_k if k is None else k
        return {"query": query, "results": self._knowledge.search(query, k)}
