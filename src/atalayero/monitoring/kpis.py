"""The KPIs of the daily batch, read from the serving database (ADR-0020).

The dashboard calls these functions and nothing else. Each one opens the serving database
read-only for one query, so that a new publication is never blocked and the next call sees it.
False positives, detection and hit rates come from the dataset's labels, in hindsight; train days
were scored by a model that saw them, so every table carries the day's `phase`.
"""

from collections.abc import Sequence

import pandas as pd

from atalayero.batch.queries import ServingUnavailableError, read_serving
from atalayero.settings import Settings

PHASES = ("warm-up", "train", "validation", "test")
__all__ = [
    "PHASES",
    "ServingUnavailableError",
    "daily_kpis",
    "drift_days",
    "queue_sources",
    "rule_kpis",
    "rule_totals",
    "summary",
    "typology_kpis",
    "typology_totals",
]


def _phases(phases: Sequence[str] | None) -> list[str]:
    chosen = list(PHASES if phases is None else phases)
    unknown = set(chosen) - set(PHASES)
    if unknown:
        raise ValueError(f"unknown phases {sorted(unknown)}; expected some of {PHASES}")
    return chosen


def daily_kpis(settings: Settings, phases: Sequence[str] | None = None) -> pd.DataFrame:
    """One row per day: the queue, its false-positive share and the detection rate."""
    return read_serving(
        settings,
        "SELECT * FROM kpi_daily WHERE list_contains($phases, phase) ORDER BY day",
        {"phases": _phases(phases)},
    )


def rule_kpis(settings: Settings, phases: Sequence[str] | None = None) -> pd.DataFrame:
    """One row per rule and day: alerts, account-days, hit rate and how many reached the queue."""
    return read_serving(
        settings,
        "SELECT * FROM kpi_rules WHERE list_contains($phases, phase) ORDER BY day, rule_id",
        {"phases": _phases(phases)},
    )


def typology_kpis(settings: Settings, phases: Sequence[str] | None = None) -> pd.DataFrame:
    """One row per typology and day: laundering transactions and how many the queue covered."""
    return read_serving(
        settings,
        "SELECT * FROM kpi_typologies WHERE list_contains($phases, phase) "
        "ORDER BY day, laundering_transactions DESC, typology",
        {"phases": _phases(phases)},
    )


def drift_days(settings: Settings) -> pd.DataFrame:
    """One row per validation or test day: drifting features and rule-alert volume against train,
    with the five most drifted features."""
    return read_serving(
        settings,
        """
        SELECT d.day, k.phase, d.compared_with, d.moderate, d.significant, d.rule_alerts,
            d.rule_alerts_reference, d.drift_detected, d.reasons, d.features[1:5] AS top_features
        FROM drift AS d JOIN days AS k USING (day)
        ORDER BY d.day
        """,
        {},
    )


# Over several days: rates are pooled (summed counts, then divided), never averaged across days.


def summary(daily: pd.DataFrame) -> dict[str, float | int | None]:
    """The headline numbers of some days of `daily_kpis`: days, alerts a day, the share of
    alerts with no laundering, and the share of laundering detected (None without laundering)."""
    queued = daily[daily["alerts"].notna()]
    alerts = int(queued["alerts"].sum())
    laundering = int(queued["laundering_transactions"].sum())
    return {
        "days": len(queued),
        "alerts_per_day": alerts / len(queued) if len(queued) else None,
        "false_positive_share": (
            1 - queued["alerts_with_laundering"].sum() / alerts if alerts else None
        ),
        "detection_rate": (
            queued["laundering_detected"].sum() / laundering if laundering else None
        ),
    }


def queue_sources(daily: pd.DataFrame) -> pd.DataFrame:
    """Each day's queue split by source: the model only, the rules only, or both."""
    both = daily["model_alerts"] + daily["rule_alerts"] - daily["alerts"]
    split = daily[["day", "phase"]].assign(
        model_only=daily["model_alerts"] - both, rules_only=daily["rule_alerts"] - both, both=both
    )
    return split.melt(
        id_vars=["day", "phase"],
        value_vars=["model_only", "rules_only", "both"],
        var_name="source",
        value_name="alerts",
    ).dropna(subset=["alerts"])


def rule_totals(rules: pd.DataFrame) -> pd.DataFrame:
    """Each rule over the days of `rule_kpis`: alerts, account-days, the share that hold
    laundering, and the share that reach the queue (the rest fall on hubs)."""
    counts = ["alerts", "account_days", "account_days_with_laundering", "account_days_in_queue"]
    totals = rules.groupby("rule_id", as_index=False)[counts].sum()
    return totals.assign(
        hit_rate=totals["account_days_with_laundering"] / totals["account_days"],
        in_queue=totals["account_days_in_queue"] / totals["account_days"],
    )


def typology_totals(typologies: pd.DataFrame) -> pd.DataFrame:
    """Each typology over the days of `typology_kpis`: laundering transactions and the share
    the queue covered, most detected first."""
    counts = ["laundering_transactions", "laundering_detected"]
    totals = typologies.groupby("typology", as_index=False)[counts].sum()
    totals = totals.assign(
        detection_rate=totals["laundering_detected"] / totals["laundering_transactions"]
    )
    return totals.sort_values(["detection_rate", "typology"], ascending=[False, True])
