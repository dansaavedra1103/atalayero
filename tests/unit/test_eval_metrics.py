from datetime import UTC, date, datetime

import pytest

from atalayero.evals.golden import CaseAnswer
from atalayero.evals.metrics import CaseResult, summarize
from atalayero.evals.run import fit_rank_threshold, score_only
from atalayero.schemas import CaseAlert, CaseReport

ANSWERS = {
    "p1": CaseAnswer(alert_id="p1", group="patterned", decision="escalate", typologies=("cycle",)),
    "p2": CaseAnswer(alert_id="p2", group="patterned", decision="escalate", typologies=("stack",)),
    "u1": CaseAnswer(alert_id="u1", group="untyped", decision="escalate", typologies=()),
    "u2": CaseAnswer(alert_id="u2", group="untyped", decision="escalate", typologies=()),
    "c1": CaseAnswer(alert_id="c1", group="clean", decision="close", typologies=()),
    "c2": CaseAnswer(alert_id="c2", group="clean", decision="close", typologies=()),
}


def _report(alert_id: str, typology: str) -> CaseReport:
    return CaseReport(
        alert_id=alert_id,
        decision="close" if typology == "none" else "escalate",
        typology=typology,
        evidence=(),
        confidence=0.5,
        narrative="",
    )


def test_every_group_is_scored_apart() -> None:
    results = [
        CaseResult(
            alert_id="p1",
            report=_report("p1", "cycle"),
            grounded=True,
            hallucinated_ids=0,
            steps=4,
            seconds=10.0,
        ),
        CaseResult(
            alert_id="p2",
            report=_report("p2", "cycle"),
            grounded=False,
            hallucinated_ids=2,
            steps=6,
            seconds=30.0,
        ),  # escalated, wrong typology
        CaseResult(alert_id="u1", report=_report("u1", "fan_in")),  # claims a typology
        CaseResult(alert_id="u2", report=None, error="timeout"),  # a failure counts as wrong
        CaseResult(alert_id="c1", report=_report("c1", "none")),
        CaseResult(alert_id="c2", report=_report("c2", "unclassified")),
    ]

    report = summarize(results, ANSWERS, "test", "dev", "sha", {}, datetime.now(UTC))

    g = report.groups
    assert (g["patterned"].decision_accuracy, g["patterned"].typology_accuracy) == (1.0, 0.5)
    assert (g["untyped"].decision_accuracy, g["untyped"].specific_typology_share) == (0.5, 0.5)
    assert g["clean"].decision_accuracy == 0.5
    assert report.primary == pytest.approx((1.0 + 0.5 + 0.5) / 3)
    assert report.failures == 1
    assert (report.grounded_share, report.hallucinated_ids_per_report) == (0.5, 1.0)
    assert (report.median_steps, report.median_seconds) == (5, 20.0)


def test_results_must_cover_the_set_once() -> None:
    results = [CaseResult(alert_id=i, report=None) for i in ANSWERS if i != "c2"]

    with pytest.raises(ValueError, match="exactly once"):
        summarize(results, ANSWERS, "test", "dev", "sha", {}, datetime.now(UTC))


def _alert(alert_id: str, rank: int) -> CaseAlert:
    return CaseAlert(
        alert_id=alert_id,
        account_key="001:A",
        day=date(2022, 9, 7),
        sources=("model",),
        score=1 / rank,
        rank=rank,
        transaction_ids=(1,),
    )


def test_the_baseline_escalates_by_rank_with_a_threshold_fit_on_dev() -> None:
    ranks = {"p1": 5, "p2": 30, "u1": 50, "u2": 400, "c1": 100, "c2": 900}
    alerts = [_alert(i, r) for i, r in ranks.items()]

    threshold = fit_rank_threshold(alerts, ANSWERS)
    results = {r.alert_id: r.report for r in score_only(alerts, threshold)}

    # 50 and 400 tie at 5/6 (400 escalates every untyped case but also c1): the smallest wins
    assert threshold == 50
    assert results["u1"].decision == "escalate" and results["u1"].typology == "unclassified"
    assert results["c1"].decision == "close" and results["c1"].typology == "none"
    assert all(not r.evidence for r in results.values())
