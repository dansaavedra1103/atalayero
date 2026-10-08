"""Drift monitoring with the population stability index (PSI).

Each day of a split is compared with train, feature by feature, with no labels. The weekly
cycle is strong in this data (ADR-0005), so weekdays are compared with the train weekdays and
weekends with the train weekend. The daily volume of rule alerts is compared with the train mean
for the same kind of day. A day drifts when a feature reaches the significant PSI or the alert
volume leaves its band; retraining on drift is left to the scheduler (phase 4).

PSI = sum over bins of (current - reference) * ln(current / reference). Bins are cut at the
reference's quantiles and closed on the right, so a point mass such as all the zeros of a count
stays in its own bin. Missing values have a bin of their own, and empty bins are floored at a
small share so the logarithm stays finite.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import duckdb
import numpy as np
import pandas as pd
from pydantic import BaseModel

from atalayero.features.tabular import CATEGORICAL
from atalayero.ingestion.source import sql_literal
from atalayero.models.data import BOOLEAN, FEATURES, Dataset, load_split
from atalayero.settings import DriftSettings, Settings, Split

logger = logging.getLogger(__name__)

FLOOR = 1e-4  # share given to an empty bin
DayKind = Literal["weekday", "weekend"]


def psi(reference: pd.Series, current: pd.Series, bins: int) -> float:
    """PSI of `current` against `reference`: by category for text and flags, by the reference's
    quantile bins otherwise."""
    if reference.name in CATEGORICAL or reference.name in BOOLEAN:
        ref = reference.astype(str).value_counts(normalize=True)
        cur = current.astype(str).value_counts(normalize=True)
        categories = ref.index.union(cur.index)
        p = ref.reindex(categories, fill_value=0).to_numpy()
        q = cur.reindex(categories, fill_value=0).to_numpy()
    else:
        ref_values = reference.to_numpy(dtype=float)
        cur_values = current.to_numpy(dtype=float)
        known = ref_values[~np.isnan(ref_values)]
        edges = (
            np.unique(np.quantile(known, np.linspace(0, 1, bins + 1)[1:-1])) if len(known) else []
        )

        def shares(values: np.ndarray) -> np.ndarray:
            missing = np.isnan(values)
            counts = np.bincount(
                np.searchsorted(edges, values[~missing], side="left"), minlength=len(edges) + 1
            )
            return np.append(counts, missing.sum()) / max(len(values), 1)

        p, q = shares(ref_values), shares(cur_values)
    p, q = np.maximum(p, FLOOR), np.maximum(q, FLOOR)
    return float(np.sum((q - p) * np.log(q / p)))


def day_kind(day: date) -> DayKind:
    return "weekend" if day.weekday() >= 5 else "weekday"


class FeatureDrift(BaseModel):
    feature: str
    psi: float


class DayDrift(BaseModel):
    day: date
    compared_with: DayKind | Literal["all"]
    features: list[FeatureDrift]  # most drifted first
    moderate: int  # features at or above the moderate PSI
    significant: int  # features at or above the significant PSI
    rule_alerts: int  # account-days the rules alerted that day
    rule_alerts_reference: float  # train mean for that kind of day
    drift_detected: bool
    reasons: list[str]


class DriftReport(BaseModel):
    split: Split
    generated_at: datetime
    days: list[DayDrift]
    drift_detected: bool

    def table(self) -> str:
        lines = [
            "| Day | Against | Moderate | Significant | Most drifted | Rule alerts (ref) | Drift |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {d.day} | {d.compared_with} | {d.moderate} | {d.significant} | "
            + ", ".join(f"{f.feature} {f.psi:.2f}" for f in d.features[:3])
            + f" | {d.rule_alerts} ({d.rule_alerts_reference:.0f}) | "
            + ("yes" if d.drift_detected else "no")
            + " |"
            for d in self.days
        ]
        return "\n".join(lines)


def _rule_alerts_per_day(settings: Settings) -> dict[date, int]:
    """Account-days the rules alerted, per day (written by `make rules`)."""
    pattern = settings.rule_alerts_dir / "part-*.parquet"
    if not any(settings.rule_alerts_dir.glob("part-*.parquet")):
        raise FileNotFoundError(f"no rule alerts in {settings.rule_alerts_dir}: run `make rules`")
    rows = duckdb.sql(
        f"SELECT triggered_at::date, count(DISTINCT account_key) "
        f"FROM read_parquet({sql_literal(str(pattern))}) GROUP BY 1"
    ).fetchall()
    return dict(rows)


def _by_day(data: Dataset) -> dict[date, pd.DataFrame]:
    assert data.days is not None
    days = pd.Series(data.days).dt.date.to_numpy()
    return {day: data.features[days == day] for day in sorted(set(days))}


Reference = tuple[DayKind | Literal["all"], pd.DataFrame, float]  # kind used, features, alerts


def drift_references(
    reference_days: Mapping[date, pd.DataFrame], alerts: Mapping[date, int]
) -> dict[DayKind, Reference]:
    """For weekdays and weekends: the train days of that kind (or every train day if there is
    none), their features, and their mean of rule-alerted account-days."""

    def reference(kind: DayKind) -> Reference:
        days = [day for day in reference_days if day_kind(day) == kind]
        used: DayKind | Literal["all"] = kind if days else "all"
        days = days or list(reference_days)
        features = pd.concat([reference_days[day] for day in days])
        return used, features, float(np.mean([alerts.get(day, 0) for day in days]))

    return {kind: reference(kind) for kind in ("weekday", "weekend")}


def day_drift(
    day: date,
    current: pd.DataFrame,
    rule_alerts: int,
    references: Mapping[DayKind, Reference],
    config: DriftSettings,
) -> DayDrift:
    """One day's features and rule-alert volume against the train reference of its kind."""
    compared_with, ref, expected = references[day_kind(day)]
    drifts = sorted(
        (
            FeatureDrift(feature=name, psi=psi(ref[name], current[name], config.bins))
            for name in FEATURES
        ),
        key=lambda d: -d.psi,
    )
    significant = [d.feature for d in drifts if d.psi >= config.significant_psi]
    reasons = [f"PSI >= {config.significant_psi}: {', '.join(significant)}"] if significant else []
    low, high = config.alert_volume_band
    if expected and not low <= rule_alerts / expected <= high:
        reasons.append(
            f"rule alerts {rule_alerts} against {expected:.0f} on train ({compared_with})"
        )
    return DayDrift(
        day=day,
        compared_with=compared_with,
        features=drifts,
        moderate=sum(d.psi >= config.moderate_psi for d in drifts),
        significant=len(significant),
        rule_alerts=rule_alerts,
        rule_alerts_reference=expected,
        drift_detected=bool(reasons),
        reasons=reasons,
    )


def detect_drift(settings: Settings, split: Split = "validation") -> DriftReport:
    """Compare each day of `split` with train; writes `<reports_dir>/drift_<split>.json`."""
    if split == "test":
        logger.warning("Reading the test split: once per reported result (ADR-0005)")
    alerts = _rule_alerts_per_day(settings)
    references = drift_references(_by_day(load_split(settings, "train")), alerts)
    days = [
        day_drift(day, current, alerts.get(day, 0), references, settings.drift)
        for day, current in _by_day(load_split(settings, split)).items()
    ]
    report = DriftReport(
        split=split,
        generated_at=datetime.now(UTC),
        days=days,
        drift_detected=any(d.drift_detected for d in days),
    )
    settings.evaluation.reports_dir.mkdir(parents=True, exist_ok=True)
    path: Path = settings.evaluation.reports_dir / f"drift_{split}.json"
    path.write_text(report.model_dump_json(indent=2))
    logger.info("Drift on %s, written to %s:\n%s", split, path, report.table())
    for d in days:
        for reason in d.reasons:
            logger.warning("%s drifts: %s", d.day, reason)
    return report
