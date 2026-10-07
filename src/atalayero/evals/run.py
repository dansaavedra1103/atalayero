"""Run a detector on a case set and write its report to `evals/reports/` (ADR-0015).

- The score-only baseline decides from the alert alone: it escalates an account-day whose rank
  by score that day is within a threshold, and closes the rest. The threshold is fit on the dev
  set, like every other choice, and it never names a specific typology.
- The agent investigates every case with the config of `config/agent.yaml` (ADR-0018). Each case
  is saved as soon as it ends, under a directory of that config version and prompts, so that a
  run can be resumed.
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from atalayero.evals.golden import CaseAnswer, CaseSet, cases_sha256, load_alerts, load_answers
from atalayero.evals.metrics import CaseResult, group_metrics, primary, summarize
from atalayero.schemas import CaseAlert, CaseReport
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

Detector = Literal["baseline", "agent"]


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


def _case_file(alert_id: str) -> str:
    return alert_id.replace(":", "_") + ".json"


def _check_versioned(settings: Settings, version: str, prompts: str) -> None:
    """A config version stands for one set of prompts: changing a prompt bumps the version."""
    for path in sorted((settings.agent_evals.dir / "reports").glob("*-agent-*.json")):
        config = json.loads(path.read_text())["config"]
        if config.get("agent_config") == version and config.get("prompts_sha256") != prompts:
            raise ValueError(
                f"{path.name} ran config/agent.yaml v{version} with other prompts: bump the version"
            )


def agent_results(
    settings: Settings, case_set: CaseSet
) -> tuple[list[CaseResult], dict[str, str | int | float]]:
    """Investigate every case of the set, resuming a run of the same config and prompts."""
    from atalayero.agent.config import load_agent_config, prompts_sha256
    from atalayero.agent.graph import Investigation
    from atalayero.agent.llm import check_pinned
    from atalayero.agent.run import investigate_alerts

    config = load_agent_config(settings.agent.config_path)
    prompts = prompts_sha256()[:12]
    _check_versioned(settings, config.version, prompts)
    digest = check_pinned(settings, config.model)
    run_dir = settings.agent_evals.runs_dir / f"{case_set}-v{config.version}-{prompts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    alerts = load_alerts(settings, case_set)

    def saved(alert: CaseAlert) -> bool:
        """Whether the case was saved whole: a crash can leave a file empty or cut short."""
        try:
            Investigation.model_validate_json((run_dir / _case_file(alert.alert_id)).read_text())
        except (FileNotFoundError, ValidationError):
            return False
        return True

    todo = [a for a in alerts if not saved(a)]
    logger.info(
        "Agent v%s on the %s set: %d of %d cases to run, in %s",
        config.version,
        case_set,
        len(todo),
        len(alerts),
        run_dir,
    )
    done = len(alerts) - len(todo)

    def save(investigation: Investigation) -> None:
        nonlocal done
        done += 1
        path = run_dir / _case_file(investigation.alert_id)
        path.write_text(investigation.model_dump_json(indent=1))
        report = investigation.report
        logger.info(
            "%d/%d %s: %s %s, grounded %s, %d tool calls, %.0f s%s",
            done,
            len(alerts),
            investigation.alert_id,
            report.decision if report else "no report",
            report.typology if report else "",
            investigation.grounding.grounded if investigation.grounding else None,
            investigation.steps,
            investigation.seconds,
            f", error: {investigation.error}" if investigation.error else "",
        )

    if todo:
        asyncio.run(investigate_alerts(settings, todo, on_result=save))
    results = []
    for alert in alerts:
        investigation = Investigation.model_validate_json(
            (run_dir / _case_file(alert.alert_id)).read_text()
        )
        grounding = investigation.grounding
        results.append(
            CaseResult(
                alert_id=alert.alert_id,
                report=investigation.report,
                grounded=grounding.grounded if grounding else False,
                hallucinated_ids=grounding.hallucinated_ids if grounding else 0,
                steps=investigation.steps,
                seconds=investigation.seconds,
                error=investigation.error,
            )
        )
    return results, {
        "agent_config": config.version,
        "model": config.model,
        "model_digest": digest[:12],
        "prompts_sha256": prompts,
        "think": str(config.think),
        "max_steps": config.max_steps,
        "max_grounding_retries": config.max_grounding_retries,
    }


def run_eval(settings: Settings, detector: Detector, case_set: CaseSet) -> Path:
    """Evaluate `detector` on `case_set`; write the report and return its path."""
    if case_set == "golden":
        logger.warning("Evaluating on the golden set (test): once per reported result (ADR-0005)")
    if detector == "agent":
        results, config = agent_results(settings, case_set)
        name = f"agent v{config['agent_config']} ({config['model']})"
        slug = f"agent-v{config['agent_config']}"
    else:
        threshold = fit_rank_threshold(load_alerts(settings, "dev"), load_answers(settings, "dev"))
        results = score_only(load_alerts(settings, case_set), threshold)
        config = {"rank_threshold": threshold, "fit_on": "dev"}
        name, slug = "score-only baseline", "score-only"
    report = summarize(
        results,
        load_answers(settings, case_set),
        detector=name,
        case_set=case_set,
        cases_sha256=cases_sha256(settings, case_set),
        config=config,
        generated_at=datetime.now(UTC),
    )
    directory = settings.agent_evals.dir / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.generated_at:%Y-%m-%d}-{case_set}-{slug}.json"
    path.write_text(report.model_dump_json(indent=2) + "\n")
    logger.info(
        "%s on the %s set, written to %s:\n%s", report.detector, case_set, path, report.table()
    )
    return path
