"""Run a detector on a case set and write its report to `evals/reports/` (ADR-0015).

The score-only baseline decides from the alert alone: it escalates an account-day whose rank by
score that day is within a threshold, and closes the rest. The threshold is fit on the dev set,
like every other choice, and it never names a specific typology.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from atalayero.evals.golden import CaseAnswer, CaseSet, cases_sha256, load_alerts, load_answers
from atalayero.evals.metrics import CaseResult, group_metrics, primary, summarize
from atalayero.schemas import CaseAlert, CaseReport
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

Detector = Literal["baseline"]


def score_only(alerts: Sequence[CaseAlert], rank_threshold: int) -> list[CaseResult]:
    """Escalate the alerts ranked within `rank_threshold` on their day; close the rest."""
    results = []
    for alert in alerts:
        escalate = alert.rank <= rank_threshold
        report = CaseReport(
            alert_id=alert.alert_id,
            decision="escalate" if escalate else "close",
            typology="unclassified" if escalate else "none",
            evidence=(),
            confidence=min(max(alert.score, 0.0), 1.0),
            narrative=(
                f"Score-only baseline: rank {alert.rank} of its day, against a threshold of "
                f"{rank_threshold}."
            ),
        )
        results.append(CaseResult(alert_id=alert.alert_id, report=report))
    return results


def fit_rank_threshold(alerts: Sequence[CaseAlert], answers: dict[str, CaseAnswer]) -> int:
    """The rank threshold with the best primary metric on `alerts` (ties: the smallest)."""
    candidates = sorted({0, *(a.rank for a in alerts)})
    best = max(
        candidates,
        key=lambda r: (primary(group_metrics(score_only(alerts, r), answers)), -r),
    )
    return best


def run_eval(settings: Settings, detector: Detector, case_set: CaseSet) -> Path:
    """Evaluate `detector` on `case_set`; write the report and return its path."""
    if case_set == "golden":
        logger.warning("Evaluating on the golden set (test): once per reported result (ADR-0005)")
    threshold = fit_rank_threshold(load_alerts(settings, "dev"), load_answers(settings, "dev"))
    results = score_only(load_alerts(settings, case_set), threshold)
    report = summarize(
        results,
        load_answers(settings, case_set),
        detector="score-only baseline",
        case_set=case_set,
        cases_sha256=cases_sha256(settings, case_set),
        config={"rank_threshold": threshold, "fit_on": "dev"},
        generated_at=datetime.now(UTC),
    )
    directory = settings.agent_evals.dir / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.generated_at:%Y-%m-%d}-{case_set}-score-only.json"
    path.write_text(report.model_dump_json(indent=2) + "\n")
    logger.info(
        "%s on the %s set (rank threshold %d), written to %s:\n%s",
        report.detector,
        case_set,
        threshold,
        path,
        report.table(),
    )
    return path
