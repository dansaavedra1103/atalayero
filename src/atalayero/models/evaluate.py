"""Alert-budget evaluation (ADR-0007).

The alert unit is the account-day: an analyst reviews everything one account sent and received on
one day. Detectors fill a daily budget of account-days:

- a model scores transactions; an account-day takes the highest score among its transactions, and
  the top N account-days of each day become alerts (ties broken by account key);
- the rules alert account-days directly: one operating point, at their own volume.

A laundering transaction is detected when the account-day of its sender or of its receiver is
alerted on the day of the transaction. Labels are read here, never upstream of the scores.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from pydantic import BaseModel, computed_field
from sklearn.metrics import average_precision_score

from atalayero.ingestion.source import sql_literal
from atalayero.rules.schema import Rule, load_rules
from atalayero.settings import Settings, Split

logger = logging.getLogger(__name__)


class Coverage(BaseModel):
    covered: int
    total: int

    @computed_field
    @property
    def rate(self) -> float:
        return self.covered / self.total if self.total else 0.0


class AlertMetrics(BaseModel):
    detector: str
    split: Split
    days: int
    budget: int | None  # alerts per day; None when the detector sets its own volume (rules)
    alerts: int
    alerts_per_day: float
    false_positive_share: float  # alerts where the account has no laundering that day
    hub_alert_share: float
    laundering: Coverage  # laundering transactions detected
    patterned: Coverage  # ... that belong to a documented attempt
    untyped: Coverage  # ... that belong to none
    attempts: Coverage  # documented attempts with at least one transaction detected
    typologies: dict[str, Coverage]  # patterned laundering, per typology
    laundering_without_hubs: Coverage  # detected through an alert on an account other than a hub


class CurvePoint(BaseModel):
    budget: int
    alerts_per_day: float
    detection: float
    detection_without_hubs: float
    false_positive_share: float


class SplitEvaluator:
    """One split's account-days and labels, in an in-memory DuckDB that attaches the warehouse
    read-only."""

    def __init__(self, settings: Settings, split: Split) -> None:
        start, end = settings.splits.bounds(split)
        self.split: Split = split
        self._start, self._end = start, end
        self.con = duckdb.connect()
        self.con.execute("SET enable_progress_bar = false")
        self.con.execute(f"ATTACH {sql_literal(str(settings.duckdb_path))} AS wh (READ_ONLY)")
        self.con.execute(
            """
            CREATE TEMP TABLE hubs AS
            SELECT sender_account_key AS account_key
            FROM wh.marts.fct_transactions
            WHERE transacted_at < $train_end
            GROUP BY ALL
            ORDER BY count(*) DESC, account_key
            LIMIT $n
            """,
            {"train_end": settings.splits.train_end, "n": settings.evaluation.hub_accounts},
        )
        self.con.execute(
            """
            CREATE TEMP TABLE transactions AS
            SELECT
                t.transaction_id,
                t.transacted_at::date AS day,
                t.sender_account_key,
                t.receiver_account_key,
                l.is_laundering,
                l.label_group,
                l.attempt_id,
                l.typology
            FROM wh.marts.fct_transactions AS t
            JOIN wh.marts.fct_laundering_labels AS l USING (transaction_id)
            WHERE t.transacted_at >= $start AND t.transacted_at < $end
            """,
            {"start": start, "end": end},
        )
        # Each account a transaction touches, on its day; a self-transfer touches one.
        self.con.execute(
            """
            CREATE TEMP TABLE legs AS
            SELECT transaction_id, day, sender_account_key AS account_key FROM transactions
            UNION
            SELECT transaction_id, day, receiver_account_key FROM transactions
            """
        )
        self.con.execute(
            """
            CREATE TEMP TABLE account_days AS
            SELECT
                day,
                account_key,
                bool_or(t.is_laundering) AS has_laundering,
                account_key IN (SELECT account_key FROM hubs) AS on_hub
            FROM legs
            JOIN transactions AS t USING (transaction_id, day)
            GROUP BY ALL
            """
        )
        self.con.execute(
            """
            CREATE TEMP TABLE laundering_legs AS
            SELECT l.transaction_id, l.day, l.account_key
            FROM legs AS l
            JOIN transactions AS t USING (transaction_id)
            WHERE t.is_laundering
            """
        )
        (self.days,) = self.con.execute("SELECT count(DISTINCT day) FROM transactions").fetchone()

    # Detectors: each one leaves its alerted account-days in the temp table `alerted`.

    def evaluate_rules(self, alerts_dir: Path, rules: Sequence[Rule]) -> AlertMetrics:
        """The alerts of `rules` (written by `make rules`) on this split, as one detector. Fails
        if the alerts come from another version of a rule."""
        if not any(alerts_dir.glob("part-*.parquet")):
            raise FileNotFoundError(f"no rule alerts in {alerts_dir}: run `make rules` first")
        versions = {rule.id: rule.version for rule in rules}
        self.con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE rule_alerts AS
            SELECT rule_id, rule_version, triggered_at::date AS day, account_key
            FROM read_parquet({sql_literal(str(alerts_dir / "part-*.parquet"))})
            WHERE triggered_at >= $start AND triggered_at < $end
                AND list_contains($ids, rule_id)
            """,
            {"start": self._start, "end": self._end, "ids": list(versions)},
        )
        stale = [
            f"{rule_id} v{version}"
            for rule_id, version in self.con.execute(
                "SELECT DISTINCT rule_id, rule_version FROM rule_alerts ORDER BY ALL"
            ).fetchall()
            if versions[rule_id] != version
        ]
        if stale:
            raise ValueError(
                f"alerts from {', '.join(stale)} differ from config/rules: run `make rules`"
            )
        self.con.execute(
            "CREATE OR REPLACE TEMP TABLE alerted AS "
            "SELECT DISTINCT day, account_key FROM rule_alerts"
        )
        detector = "rules " + ", ".join(f"{i} v{v}" for i, v in sorted(versions.items()))
        return self._metrics(detector, budget=None)

    def rule_budget(self) -> dict[date, int]:
        """Alerts per day of the last `evaluate_rules`, to compare models at the same volume."""
        rows = self.con.execute(
            "SELECT day, count(DISTINCT account_key) FROM rule_alerts GROUP BY day"
        ).fetchall()
        return dict(rows)

    def set_scores(self, transaction_ids: np.ndarray, scores: np.ndarray) -> None:
        """A model's scores, one per transaction of the split (others are ignored); ranks the
        account-days of each day by their highest score."""
        self.con.register("scores_input", {"transaction_id": transaction_ids, "score": scores})
        self.con.execute(
            """
            CREATE OR REPLACE TEMP TABLE scores AS
            SELECT transaction_id, score
            FROM scores_input
            WHERE transaction_id IN (SELECT transaction_id FROM transactions)
            """
        )
        self.con.unregister("scores_input")
        (missing,) = self.con.execute(
            "SELECT count(*) FROM transactions ANTI JOIN scores USING (transaction_id)"
        ).fetchone()
        if missing:
            raise ValueError(f"{missing} transactions of the {self.split} split have no score")
        self.con.execute(
            """
            CREATE OR REPLACE TEMP TABLE ranked AS
            SELECT
                day,
                account_key,
                score,
                row_number() OVER (PARTITION BY day ORDER BY score DESC, account_key) AS rank
            FROM (
                SELECT day, account_key, max(score) AS score
                FROM legs JOIN scores USING (transaction_id)
                GROUP BY ALL
            )
            """
        )

    def evaluate_scores(self, detector: str, budget: int | Mapping[date, int]) -> AlertMetrics:
        """The top `budget` account-days of each day (or a budget per day) by the scores set
        with `set_scores`."""
        if isinstance(budget, Mapping):  # a day missing from the mapping gets no alerts
            days, sizes, label = list(budget), list(budget.values()), None
        else:
            days = [d for (d,) in self.con.execute("SELECT DISTINCT day FROM ranked").fetchall()]
            sizes, label = [budget] * len(days), budget
        self.con.execute(
            """
            CREATE OR REPLACE TEMP TABLE alerted AS
            SELECT r.day, r.account_key
            FROM ranked AS r
            JOIN (SELECT unnest($days::DATE[]) AS day, unnest($sizes::INTEGER[]) AS size) AS b
                USING (day)
            WHERE r.rank <= b.size
            """,
            {"days": days, "sizes": sizes},
        )
        return self._metrics(detector, budget=label)

    def pr_auc(self) -> float:
        """Average precision of the scores over the split's transactions (labels per
        transaction, no budget)."""
        data = self.con.execute(
            "SELECT t.is_laundering, s.score FROM transactions AS t JOIN scores AS s "
            "USING (transaction_id)"
        ).fetchnumpy()
        return float(average_precision_score(data["is_laundering"], data["score"]))

    def curve(self, budgets: Iterable[int]) -> list[CurvePoint]:
        """Detection and false positives of the scores at each daily budget."""
        rows = self.con.execute(
            """
            WITH best AS (  -- the better rank of each laundering transaction's two accounts
                SELECT
                    ll.transaction_id,
                    min(r.rank) AS rank,
                    min(r.rank) FILTER (WHERE NOT a.on_hub) AS rank_other_than_hub
                FROM laundering_legs AS ll
                JOIN ranked AS r USING (day, account_key)
                JOIN account_days AS a USING (day, account_key)
                GROUP BY ALL
            ),
            alerts AS (
                SELECT r.rank, a.has_laundering
                FROM ranked AS r JOIN account_days AS a USING (day, account_key)
            )
            SELECT
                b.budget,
                (SELECT count(*) FROM alerts WHERE rank <= b.budget) AS alerts,
                (SELECT count(*) FROM alerts WHERE rank <= b.budget AND NOT has_laundering)
                    AS false_positives,
                (SELECT count(*) FROM best WHERE rank <= b.budget) AS detected,
                (SELECT count(*) FROM best WHERE rank_other_than_hub <= b.budget)
                    AS detected_without_hubs,
                (SELECT count(*) FROM transactions WHERE is_laundering) AS laundering
            FROM (SELECT unnest($budgets) AS budget) AS b
            ORDER BY b.budget
            """,
            {"budgets": sorted(set(budgets))},
        ).fetchall()
        return [
            CurvePoint(
                budget=budget,
                alerts_per_day=alerts / self.days,
                detection=detected / laundering if laundering else 0.0,
                detection_without_hubs=without_hubs / laundering if laundering else 0.0,
                false_positive_share=false_positives / alerts if alerts else 0.0,
            )
            for budget, alerts, false_positives, detected, without_hubs, laundering in rows
        ]

    def detections(self) -> pd.DataFrame:
        """The split's laundering transactions and whether the last evaluated detector caught
        them, with what an error analysis looks at."""
        return self.con.execute(
            """
            SELECT
                d.transaction_id,
                t.transacted_at,
                t.sender_account_key,
                t.receiver_account_key,
                t.amount_paid_usd,
                t.payment_format,
                d.label_group,
                d.attempt_id,
                d.typology,
                d.detected,
                d.via_other_than_hub
            FROM detected AS d
            JOIN wh.marts.fct_transactions AS t USING (transaction_id)
            ORDER BY d.transaction_id
            """
        ).df()

    def alerts(self) -> pd.DataFrame:
        """The account-days the last evaluated detector alerted, with their number of
        transactions."""
        return self.con.execute(
            """
            SELECT day, account_key, a.has_laundering, a.on_hub, count(*) AS transactions
            FROM alerted
            JOIN account_days AS a USING (day, account_key)
            JOIN legs USING (day, account_key)
            GROUP BY ALL
            ORDER BY day, account_key
            """
        ).df()

    def _metrics(self, detector: str, budget: int | None) -> AlertMetrics:
        alerts, false_positives, on_hubs = self.con.execute(
            """
            SELECT
                count(*),
                count(*) FILTER (WHERE NOT a.has_laundering),
                count(*) FILTER (WHERE a.on_hub)
            FROM alerted JOIN account_days AS a USING (day, account_key)
            """
        ).fetchone()
        self.con.execute(
            """
            CREATE OR REPLACE TEMP TABLE detected AS
            SELECT
                t.transaction_id,
                t.label_group,
                t.attempt_id,
                t.typology,
                d.transaction_id IS NOT NULL AS detected,
                coalesce(d.via_other_than_hub, false) AS via_other_than_hub
            FROM transactions AS t
            LEFT JOIN (
                SELECT ll.transaction_id, bool_or(NOT a.on_hub) AS via_other_than_hub
                FROM laundering_legs AS ll
                JOIN alerted USING (day, account_key)
                JOIN account_days AS a USING (day, account_key)
                GROUP BY ALL
            ) AS d USING (transaction_id)
            WHERE t.is_laundering
            """
        )

        def coverage(where: str = "true", count: str = "*") -> Coverage:
            covered, total = self.con.execute(
                f"SELECT count({count}) FILTER (WHERE detected), count({count}) "
                f"FROM detected WHERE {where}"
            ).fetchone()
            return Coverage(covered=covered, total=total)

        typologies = self.con.execute(
            "SELECT typology, count(*) FILTER (WHERE detected), count(*) FROM detected "
            "WHERE typology IS NOT NULL GROUP BY typology ORDER BY typology"
        ).fetchall()
        without_hubs, laundering = self.con.execute(
            "SELECT count(*) FILTER (WHERE via_other_than_hub), count(*) FROM detected"
        ).fetchone()
        return AlertMetrics(
            detector=detector,
            split=self.split,
            days=self.days,
            budget=budget,
            alerts=alerts,
            alerts_per_day=alerts / self.days if self.days else 0.0,
            false_positive_share=false_positives / alerts if alerts else 0.0,
            hub_alert_share=on_hubs / alerts if alerts else 0.0,
            laundering=coverage(),
            patterned=coverage("label_group = 'patterned'"),
            untyped=coverage("label_group = 'untyped'"),
            attempts=coverage("attempt_id IS NOT NULL", "DISTINCT attempt_id"),
            typologies={t: Coverage(covered=c, total=n) for t, c, n in typologies},
            laundering_without_hubs=Coverage(covered=without_hubs, total=laundering),
        )


class EvaluationReport(BaseModel):
    split: Split
    generated_at: datetime
    results: list[AlertMetrics]
    curves: dict[str, list[CurvePoint]] = {}
    pr_auc: dict[str, float] = {}

    def table(self) -> str:
        """The results as a Markdown table."""
        lines = [
            "| Detector | Alerts/day | False positives | Detection | Without hubs | Attempts "
            "| On hubs |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {m.detector} | {m.alerts_per_day:.1f} | {m.false_positive_share:.0%} "
            f"| {m.laundering.rate:.1%} | {m.laundering_without_hubs.rate:.1%} "
            f"| {m.attempts.covered}/{m.attempts.total} | {m.hub_alert_share:.0%} |"
            for m in self.results
        ]
        return "\n".join(lines)


def evaluate_rules_only(settings: Settings, split: Split) -> Path:
    """The rules-only baseline on a split: all rules together, then each one alone. Writes
    `<reports_dir>/rules_only_<split>.json` and returns its path."""
    if split == "test":
        logger.warning("Reading the test split: once per reported result (ADR-0005)")
    evaluator = SplitEvaluator(settings, split)
    rules = load_rules(settings.rules_dir)
    results = [evaluator.evaluate_rules(settings.rule_alerts_dir, rules)]
    for rule in rules:
        results.append(evaluator.evaluate_rules(settings.rule_alerts_dir, [rule]))
    report = EvaluationReport(split=split, generated_at=datetime.now(UTC), results=results)
    settings.evaluation.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.evaluation.reports_dir / f"rules_only_{split}.json"
    path.write_text(report.model_dump_json(indent=2))
    logger.info("Rules-only baseline on %s, written to %s:\n%s", split, path, report.table())
    return path
