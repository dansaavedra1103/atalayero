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
    "rule_kpis",
    "typology_kpis",
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
