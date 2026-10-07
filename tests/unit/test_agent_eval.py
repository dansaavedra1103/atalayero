import json
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import pytest

from atalayero.agent.config import load_agent_config
from atalayero.agent.graph import Investigation
from atalayero.agent.grounding import Grounding
from atalayero.evals.golden import CaseAnswer
from atalayero.evals.metrics import AgentEvalReport
from atalayero.evals.run import run_eval
from atalayero.schemas import CaseAlert, CaseReport, Evidence
from atalayero.settings import Settings

CONFIG = load_agent_config(Settings().agent.config_path)  # the live config/agent.yaml

CASES = {  # alert ID -> (group, expected decision, what the fake agent concludes)
    "2022-09-07:001:P": ("patterned", "escalate", ("escalate", "fan_in")),
    "2022-09-07:001:U": ("untyped", "escalate", ("close", "none")),
    "2022-09-07:001:C": ("clean", "close", ("close", "none")),
}


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    base = Settings()
    settings = base.model_copy(
        update={
            "agent_evals": base.agent_evals.model_copy(
                update={"dir": tmp_path / "evals", "runs_dir": tmp_path / "runs"}
            )
        }
    )
    directory = settings.agent_evals.dir
    directory.mkdir()
    alerts, answers = [], []
    for alert_id, (group, decision, _) in CASES.items():
        account = alert_id.split(":", 1)[1]
        alerts.append(
            CaseAlert(
                alert_id=alert_id,
                account_key=account,
                day=date(2022, 9, 7),
                sources=("model",),
                score=0.1,
                rank=10,
                transaction_ids=(1,),
            )
        )
        typologies = ("fan_in",) if group == "patterned" else ()
        answers.append(
            CaseAnswer(alert_id=alert_id, group=group, decision=decision, typologies=typologies)
        )
    for name, rows in (("dev_alerts", alerts), ("dev_answers", answers)):
        (directory / f"{name}.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in rows))
    monkeypatch.setattr("atalayero.agent.llm.check_pinned", lambda s, m: "d" * 64)
    return settings


def _investigation(alert: CaseAlert) -> Investigation:
    decision, typology = CASES[alert.alert_id][2]
    evidence = (Evidence(transaction_id=1, amount_usd=10.0),) if decision == "escalate" else ()
    return Investigation(
        alert_id=alert.alert_id,
        report=CaseReport(
            alert_id=alert.alert_id,
            decision=decision,
            typology=typology,
            evidence=evidence,
            confidence=0.6,
            narrative="",
        ),
        grounding=Grounding(grounded=True, hallucinated_ids=0, problems=[]),
        steps=3,
        llm_calls=5,
        retries=0,
        tokens=100,
        seconds=2.0,
        config_version=CONFIG.version,
        prompts_sha256="x",
        transcript=[],
    )


def fake_agent(ran: list[str], crash_after: int | None = None) -> Callable:
    async def investigate(
        settings: Settings, alerts: Sequence[CaseAlert], on_result: Callable
    ) -> list[Investigation]:
        for i, alert in enumerate(alerts):
            if crash_after is not None and i == crash_after:
                raise RuntimeError("interrupted")
            ran.append(alert.alert_id)
            on_result(_investigation(alert))
        return []

    return investigate


def test_an_agent_run_resumes_and_is_scored(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    first: list[str] = []
    monkeypatch.setattr("atalayero.agent.run.investigate_alerts", fake_agent(first, crash_after=1))
    with pytest.raises(RuntimeError, match="interrupted"):
        run_eval(settings, "agent", "dev")
    again: list[str] = []
    monkeypatch.setattr("atalayero.agent.run.investigate_alerts", fake_agent(again))

    path = run_eval(settings, "agent", "dev")

    assert len(first) == 1 and set(first + again) == set(CASES) and not set(first) & set(again)
    report = AgentEvalReport.model_validate_json(path.read_text())
    assert path.name.endswith(f"-dev-agent-v{CONFIG.version}.json")
    assert (
        report.config["agent_config"] == CONFIG.version and report.config["model"] == CONFIG.model
    )
    assert report.primary == pytest.approx(2 / 3)  # patterned and clean right, untyped wrong
    assert report.groups["patterned"].typology_accuracy == 1.0
    assert (report.grounded_share, report.median_steps) == (1.0, 3)


def test_a_version_runs_with_one_set_of_prompts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = settings.agent_evals.dir / "reports"
    reports.mkdir()
    (reports / f"2026-10-06-dev-agent-v{CONFIG.version}.json").write_text(
        json.dumps({"config": {"agent_config": CONFIG.version, "prompts_sha256": "other"}})
    )
    monkeypatch.setattr("atalayero.agent.run.investigate_alerts", fake_agent([]))

    with pytest.raises(ValueError, match="bump the version"):
        run_eval(settings, "agent", "dev")


def test_a_case_cut_short_by_a_crash_runs_again(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("atalayero.agent.run.investigate_alerts", fake_agent([]))
    run_eval(settings, "agent", "dev")
    next(settings.agent_evals.runs_dir.glob("*/2022-09-07_001_U.json")).write_text("")
    again: list[str] = []
    monkeypatch.setattr("atalayero.agent.run.investigate_alerts", fake_agent(again))

    run_eval(settings, "agent", "dev")

    assert again == ["2022-09-07:001:U"]
