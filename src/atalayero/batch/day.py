"""The daily batch (ADR-0020): the simulation replayed one day at a time.

Each day has a directory under `<batch.dir>/days/` with:
- `features.parquet`: the point-in-time features of the day's transactions (ADR-0008), no labels;
- `rule_alerts.parquet`: the rules' alerts triggered that day;
- `scores.parquet`: the champion's score of each transaction, from `models.train_start` on;
- `alerts.parquet`: the day's alert queue: the account-days the rules alerted or the model ranked
  within `batch.alert_budget`, hubs left out (as the agent's case sets, ADR-0015);
- `drift.json`: the day against train, on validation and test days;
- `state/`: the motif pass and the rules as the day left them, for the next day to carry on;
- `manifest.json`: what was written, by which rules and champion. It is written last.

The tabular and graph features of a day depend on four days of history at most, read from the
warehouse. The motif features and the rules' alerts depend on more than any window of history
holds: relay depths and cooldowns build on earlier ones, and when several chains tie, which one a
search finds depends on the order the state was built in. So a day carries on from the state the
day before it left, runs only once that day is complete, and gives the same result as a single
pass over the whole period. Every file is replaced atomically and the manifest goes last: a day is
complete when its manifest exists, and running it again replaces it.
"""

import gzip
import logging
import os
import pickle
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb
import pandas as pd
from pydantic import BaseModel
from sklearn.pipeline import Pipeline

from atalayero.evals.golden import queue_cases
from atalayero.features.graph import graph_features
from atalayero.features.motifs import MotifState, motif_features_frame
from atalayero.features.tabular import MEMORY_LIMIT, tabular_features_sql
from atalayero.ingestion.source import sql_literal
from atalayero.models.data import model_inputs
from atalayero.models.evaluate import HUBS_SQL
from atalayero.models.families import transaction_scores
from atalayero.monitoring.drift import (
    DayDrift,
    DayKind,
    Reference,
    day_drift,
    drift_references,
)
from atalayero.rules.online import OnlineEvaluator
from atalayero.rules.schema import Rule, load_rules
from atalayero.schemas import Alert, DayPhase
from atalayero.settings import Settings
from atalayero.streaming.consumer import write_alerts
from atalayero.streaming.producer import iter_transactions

logger = logging.getLogger(__name__)

FEATURE_LOOKBACK = timedelta(hours=96)  # the longest look-back of any feature (ADR-0013)


class MissingDaysError(RuntimeError):
    """A day needs earlier days that are not complete."""


class ChampionRef(BaseModel):
    version: str
    family: str


class DayManifest(BaseModel):
    day: date
    phase: DayPhase
    transactions: int
    rules: dict[str, str]  # rule ID -> version
    rule_alerts: int
    champion: ChampionRef | None  # None before `models.train_start`: not scored
    alerts: int | None  # account-days in the queue
    drift_detected: bool | None  # None outside validation and test
    written_at: datetime


def _start(day: date) -> datetime:
    return datetime.combine(day, time())


def day_dir(settings: Settings, day: date) -> Path:
    return settings.batch.dir / "days" / day.isoformat()


def load_manifest(settings: Settings, day: date) -> DayManifest | None:
    """The day's manifest, or None if the day is not complete."""
    path = day_dir(settings, day) / "manifest.json"
    return DayManifest.model_validate_json(path.read_text()) if path.exists() else None


def batch_days(settings: Settings) -> list[date]:
    """Every day of the simulation the batch covers: from the first transaction to the end of
    the test split (ADR-0005)."""
    with duckdb.connect(str(settings.duckdb_path), read_only=True) as con:
        (first,) = con.execute(
            "SELECT min(transacted_at)::date FROM marts.fct_transactions"
        ).fetchone()
    last = settings.splits.test_end.date() - timedelta(days=1)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def day_phase(settings: Settings, day: date) -> DayPhase:
    start, splits = _start(day), settings.splits
    if start < settings.models.train_start:
        return "warm-up"
    if start < splits.train_end:
        return "train"
    if start < splits.validation_end:
        return "validation"
    if start < splits.test_end:
        return "test"
    raise ValueError(f"{day} is past the end of the test split, which is never used (ADR-0005)")


def _replace(path: Path, write: Callable[[Path], None]) -> None:
    """Write a file next to `path` and move it into place, so that no reader sees half of it."""
    tmp = path.with_name(f".{path.name}.tmp")
    write(tmp)
    os.replace(tmp, path)


class Batch:
    """The batch over one warehouse: the rules, the champion and the drift reference, loaded once
    for every day it runs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rules: list[Rule] = load_rules(settings.rules_dir)
        self.days = batch_days(settings)
        self._champion: tuple[ChampionRef, Pipeline] | None = None
        self._references: dict[DayKind, Reference] | None = None

    # Dependencies on earlier days

    def _require(self, days: Sequence[date], reason: str) -> list[DayManifest]:
        manifests = {d: load_manifest(self.settings, d) for d in days}
        missing = [d.isoformat() for d, m in manifests.items() if m is None]
        if missing:
            raise MissingDaysError(f"{reason} needs {', '.join(missing)}: run those days first")
        return [m for m in manifests.values() if m is not None]

    def _previous(self, day: date) -> DayManifest | None:
        """The complete day before `day`, or None for the first day of the simulation."""
        if day == self.days[0]:
            return None
        (previous,) = self._require([day - timedelta(days=1)], f"{day}")
        return previous

    def _carried(self, previous: DayManifest | None, name: str) -> object | None:
        """The state the day before left, or None on the first day."""
        if previous is None:
            return None
        with gzip.open(day_dir(self.settings, previous.day) / "state" / name, "rb") as f:
            return pickle.load(f)  # noqa: S301 (written by the batch itself, under data/)

    @staticmethod
    def _carry(directory: Path, name: str, state: object) -> None:
        (directory / "state").mkdir(exist_ok=True)

        def write(tmp: Path) -> None:
            with gzip.open(tmp, "wb", compresslevel=1) as f:
                pickle.dump(state, f, protocol=5)

        _replace(directory / "state" / name, write)

    # Steps

    def _features(
        self, con: duckdb.DuckDBPyConnection, day: date, previous: DayManifest | None
    ) -> int:
        directory = day_dir(self.settings, day)
        start = _start(day)
        since, end = start - FEATURE_LOOKBACK, start + timedelta(days=1)
        con.execute(
            "CREATE OR REPLACE TEMP VIEW history AS SELECT * FROM wh.marts.fct_transactions "
            f"WHERE transacted_at >= TIMESTAMP {sql_literal(since.isoformat(sep=' '))} "
            f"AND transacted_at < TIMESTAMP {sql_literal(end.isoformat(sep=' '))}"
        )
        boundary = f"TIMESTAMP {sql_literal(start.isoformat(sep=' '))}"
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW today AS SELECT * FROM history "
            f"WHERE transacted_at >= {boundary}"
        )
        con.sql(
            f"SELECT * FROM ({tabular_features_sql('history')}) WHERE transacted_at >= {boundary}"
        ).create_view("day_tabular")
        graph_features(con, "history", days=[day]).create_view("day_graph")
        state = self._carried(previous, "motifs.pkl.gz")
        state = state if isinstance(state, MotifState) else MotifState()
        motif_features_frame(con, "today", state).create_view("day_motifs")
        joined = con.sql(
            """
            SELECT t.*, g.* EXCLUDE (transaction_id), m.* EXCLUDE (transaction_id)
            FROM day_tabular AS t
            JOIN day_graph AS g USING (transaction_id)
            JOIN day_motifs AS m USING (transaction_id)
            ORDER BY t.transaction_id
            """
        )
        out = directory / "features.parquet"
        _replace(out, lambda tmp: joined.write_parquet(str(tmp)))
        self._carry(directory, "motifs.pkl.gz", state)
        (rows,) = con.execute(
            f"SELECT count(*) FROM read_parquet({sql_literal(str(out))})"
        ).fetchone()
        return int(rows)

    def _rule_alerts(self, day: date, previous: DayManifest | None) -> list[Alert]:
        versions = {rule.id: rule.version for rule in self.rules}
        if previous is not None and previous.rules != versions:
            raise MissingDaysError(
                f"{previous.day} ran with other rule versions than config/rules: replay the days "
                "from the first one"
            )
        evaluator = self._carried(previous, "rules.pkl.gz")
        if not isinstance(evaluator, OnlineEvaluator):
            evaluator = OnlineEvaluator(self.rules)
        start = _start(day)
        alerts = [
            alert
            for tx in iter_transactions(
                self.settings.duckdb_path, since=start, until=start + timedelta(days=1)
            )
            for alert in evaluator.observe(tx)
        ]
        directory = day_dir(self.settings, day)
        _replace(directory / "rule_alerts.parquet", lambda tmp: write_alerts(alerts, tmp))
        self._carry(directory, "rules.pkl.gz", evaluator)
        return alerts

    def _champion_model(self) -> tuple[ChampionRef, Pipeline]:
        if self._champion is None:
            from atalayero.models.registry import Registry  # MLflow is slow to import

            registry = Registry(self.settings)
            version = registry.champion_version()
            ref = ChampionRef(version=version, family=registry.champion_family())
            self._champion = ref, registry.load_version(version)
            logger.info("Scoring with the champion: %s v%s", ref.family, ref.version)
        return self._champion

    def _scores(self, features: Path, out: Path) -> ChampionRef:
        ref, model = self._champion_model()
        frame = duckdb.read_parquet(str(features)).df()
        scores = pd.DataFrame(
            {
                "transaction_id": frame["transaction_id"].astype("int64"),
                "score": transaction_scores(model, model_inputs(frame)),
            }
        )
        _replace(out, lambda tmp: scores.to_parquet(tmp, index=False))
        return ref

    def _queue(self, con: duckdb.DuckDBPyConnection, day: date, directory: Path) -> int:
        """The day's alert queue, ranked as `SplitEvaluator.ranking` ranks account-days."""
        scores = sql_literal(str(directory / "scores.parquet"))
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE hubs AS {HUBS_SQL}",
            {
                "train_end": self.settings.splits.train_end,
                "n": self.settings.evaluation.hub_accounts,
            },
        )
        ranking = con.execute(
            f"""
            WITH legs AS (
                SELECT transaction_id, transacted_at::date AS day, sender_account_key AS account_key
                FROM today
                UNION
                SELECT transaction_id, transacted_at::date, receiver_account_key FROM today
            ),
            scored AS (SELECT * FROM legs JOIN read_parquet({scores}) USING (transaction_id)),
            account_days AS (
                SELECT
                    day,
                    account_key,
                    max(score) AS score,
                    list(transaction_id ORDER BY score DESC, transaction_id)[1:$top]
                        AS top_transactions
                FROM scored
                GROUP BY day, account_key
            )
            SELECT
                day,
                account_key,
                score,
                row_number() OVER (PARTITION BY day ORDER BY score DESC, account_key) AS rank,
                account_key IN (SELECT account_key FROM hubs) AS on_hub,
                top_transactions
            FROM account_days
            ORDER BY day, rank
            """,
            {"top": self.settings.agent_evals.top_transactions},
        ).df()
        hits = duckdb.read_parquet(str(directory / "rule_alerts.parquet")).df()
        hits = hits.assign(day=hits["triggered_at"].dt.date)[
            ["rule_id", "day", "account_key", "transaction_id"]
        ]
        queue = queue_cases(ranking, hits, self.settings.batch.alert_budget)
        queue["sources"] = queue["sources"].map(list)
        queue["transaction_ids"] = queue["transaction_ids"].map(list)
        _replace(directory / "alerts.parquet", lambda tmp: queue.to_parquet(tmp, index=False))
        return len(queue)

    def _drift_references(self) -> dict[DayKind, Reference]:
        """Train's features and rule-alert volume per kind of day, from the train days' files."""
        if self._references is None:
            splits, start = self.settings.splits, self.settings.models.train_start
            train = [d for d in self.days if start <= _start(d) < splits.train_end]
            self._require(train, "the drift reference")
            features, alerts = {}, {}
            for d in train:
                directory = day_dir(self.settings, d)
                frame = duckdb.read_parquet(str(directory / "features.parquet")).df()
                features[d] = model_inputs(frame)
                alerts[d] = len(
                    {a.account_key for a in read_alerts(directory / "rule_alerts.parquet")}
                )
            self._references = drift_references(features, alerts)
        return self._references

    def _drift(self, day: date, directory: Path, rule_alerts: Sequence[Alert]) -> bool:
        current = model_inputs(duckdb.read_parquet(str(directory / "features.parquet")).df())
        drift = day_drift(
            day,
            current,
            len({a.account_key for a in rule_alerts}),
            self._drift_references(),
            self.settings.drift,
        )
        _replace(
            directory / "drift.json", lambda tmp: tmp.write_text(drift.model_dump_json(indent=2))
        )
        for reason in drift.reasons:
            logger.warning("%s drifts: %s", day, reason)
        return drift.drift_detected

    # Running

    def run_day(self, day: date) -> DayManifest:
        """Run every step of the day and write its manifest; replaces an earlier run of it."""
        if day not in self.days:
            raise ValueError(f"{day} is outside the simulation ({self.days[0]}–{self.days[-1]})")
        phase = day_phase(self.settings, day)
        directory = day_dir(self.settings, day)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").unlink(missing_ok=True)  # incomplete until the end
        for stale in ("scores.parquet", "alerts.parquet", "drift.json"):
            (directory / stale).unlink(missing_ok=True)
        with duckdb.connect() as con:
            con.execute("SET enable_progress_bar = false")
            con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
            con.execute(f"SET temp_directory = {sql_literal(str(self.settings.data_dir / 'tmp'))}")
            con.execute(f"ATTACH {sql_literal(str(self.settings.duckdb_path))} AS wh (READ_ONLY)")
            previous = self._previous(day)
            transactions = self._features(con, day, previous)
            rule_alerts = self._rule_alerts(day, previous)
            champion = alerts = drift = None
            if phase != "warm-up":
                champion = self._scores(
                    directory / "features.parquet", directory / "scores.parquet"
                )
                alerts = self._queue(con, day, directory)
            if phase in ("validation", "test"):
                drift = self._drift(day, directory, rule_alerts)
        manifest = DayManifest(
            day=day,
            phase=phase,
            transactions=transactions,
            rules={rule.id: rule.version for rule in self.rules},
            rule_alerts=len(rule_alerts),
            champion=champion,
            alerts=alerts,
            drift_detected=drift,
            written_at=datetime.now(UTC),
        )
        _replace(
            directory / "manifest.json",
            lambda tmp: tmp.write_text(manifest.model_dump_json(indent=2)),
        )
        logger.info(
            "%s (%s): %d transactions, %d rule alerts, %s in the queue%s",
            day,
            phase,
            transactions,
            len(rule_alerts),
            "none" if alerts is None else alerts,
            "" if drift is None else f", drift {'detected' if drift else 'none'}",
        )
        return manifest

    def replay(self, first: date | None = None, last: date | None = None) -> list[DayManifest]:
        """Run the days from `first` to `last` (the whole simulation by default), in order."""
        days = [
            d for d in self.days if (first is None or d >= first) and (last is None or d <= last)
        ]
        return [self.run_day(day) for day in days]


def day_drifted(settings: Settings, day: date) -> bool:
    """Whether a complete day drifted from train; False for a day with no drift check (warm-up
    and train days). Fails if the day is not complete."""
    manifest = load_manifest(settings, day)
    if manifest is None:
        raise MissingDaysError(f"{day} is not complete: run it first")
    if manifest.drift_detected:
        drift = DayDrift.model_validate_json((day_dir(settings, day) / "drift.json").read_text())
        for reason in drift.reasons:
            logger.warning("%s drifts: %s", day, reason)
    else:
        logger.info(
            "%s: %s", day, "no drift" if manifest.drift_detected is False else "not checked"
        )
    return bool(manifest.drift_detected)


def read_alerts(path: Path) -> list[Alert]:
    """The alerts in a file written by `write_alerts`."""
    relation = duckdb.read_parquet(str(path))
    return [
        Alert(**{**dict(zip(relation.columns, row, strict=True)), "evidence": tuple(row[-1])})
        for row in relation.fetchall()
    ]


def manifests(settings: Settings) -> list[DayManifest]:
    """The manifests of every complete day, by day."""
    root = settings.batch.dir / "days"
    found = [
        DayManifest.model_validate_json(path.read_text())
        for path in sorted(root.glob("*/manifest.json"))
    ]
    return sorted(found, key=lambda m: m.day)
