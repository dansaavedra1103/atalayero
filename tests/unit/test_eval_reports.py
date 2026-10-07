"""The versioned case sets and reports in `evals/` (CLAUDE.md rule 9: CI validates reports)."""

import json
from pathlib import Path

import pytest

from atalayero.agent.config import load_agent_config
from atalayero.evals.golden import CaseAnswer, load_alerts, load_answers
from atalayero.evals.metrics import AgentEvalReport
from atalayero.evals.run import fit_rank_threshold
from atalayero.schemas import CaseAlert
from atalayero.settings import Settings

EVALS = Path(__file__).parents[2] / "evals"
REPORTS = sorted((EVALS / "reports").glob("*.json"))
SETS = ("dev", "golden")


@pytest.mark.parametrize("path", REPORTS, ids=[p.name for p in REPORTS])
def test_every_report_matches_the_schema(path: Path) -> None:
    report = AgentEvalReport.model_validate_json(path.read_text())

    assert path.name.startswith(f"{report.generated_at:%Y-%m-%d}-{report.case_set}-")


@pytest.mark.parametrize("case_set", SETS)
def test_case_sets_pair_alerts_with_answers(case_set: str) -> None:
    alerts_path, answers_path = (
        EVALS / f"{case_set}_alerts.jsonl",
        EVALS / f"{case_set}_answers.jsonl",
    )
    if not alerts_path.exists():
        pytest.skip(f"no {case_set} set yet")

    lines = alerts_path.read_text().splitlines()
    alerts = [CaseAlert.model_validate_json(line) for line in lines]
    answers = [
        CaseAnswer.model_validate_json(line) for line in answers_path.read_text().splitlines()
    ]

    assert [a.alert_id for a in alerts] == [a.alert_id for a in answers]
    assert all(set(json.loads(line)) == set(CaseAlert.model_fields) for line in lines)


def test_the_agent_starts_from_the_baselines_call() -> None:
    """ADR-0019: the agent's starting point is the score-only baseline's threshold on dev."""
    settings = Settings()
    threshold = fit_rank_threshold(load_alerts(settings, "dev"), load_answers(settings, "dev"))

    assert load_agent_config(settings.agent.config_path).model_call_rank == threshold
