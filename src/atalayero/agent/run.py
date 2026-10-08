"""Run the investigator on alerts, with its MCP server as a subprocess over stdio (ADR-0018): on
the case sets, or on the top of the batch's queue of a day (ADR-0024)."""

import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from mcp import Client, StdioServerParameters

from atalayero.agent.config import load_agent_config
from atalayero.agent.graph import Investigation, Investigator
from atalayero.agent.llm import check_pinned, ollama_chat
from atalayero.agent.tools import ToolBox
from atalayero.batch.queries import investigation_path
from atalayero.rules.schema import load_rules
from atalayero.schemas import CaseAlert
from atalayero.settings import Settings

logger = logging.getLogger(__name__)


async def investigate_alerts(
    settings: Settings,
    alerts: Sequence[CaseAlert],
    on_result: Callable[[Investigation], None] | None = None,
    batch_day: date | None = None,
) -> list[Investigation]:
    """Investigate `alerts` one after the other, with the pinned model of `config/agent.yaml`:
    alerts of the case sets, or of the batch's queue on `batch_day`."""
    config = load_agent_config(settings.agent.config_path)
    check_pinned(settings, config.model)
    toolbox = ToolBox(settings, alerts)  # the grounding check's own view of the warehouse
    day = [] if batch_day is None else ["--day", batch_day.isoformat()]
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "atalayero.mcp_server", *day], env=dict(os.environ)
    )
    results = []
    async with Client(server) as client:
        investigator = Investigator(
            config,
            ollama_chat(settings, config),
            client,
            toolbox.lookup,
            toolbox.cutoff,
            load_rules(settings.rules_dir),
        )
        for alert in alerts:
            result = await investigator.investigate(alert)
            results.append(result)
            if on_result is not None:
                on_result(result)
    return results


async def investigate_day(settings: Settings, day: date, top: int) -> list[Investigation]:
    """Investigate the first `top` alerts of the batch's queue on `day`, by rank, and keep each
    investigation beside the day, where the API and the dashboard read it."""
    from atalayero.batch.day import day_alerts

    queued = day_alerts(settings, day)[:top]
    if not queued:
        raise ValueError(f"{day} has no alert queue")
    # The investigator sees an alert as it does on the case sets: the day's phase is not its call.
    alerts = [CaseAlert(**alert.model_dump(exclude={"phase"})) for alert in queued]
    return await investigate_alerts(
        settings,
        alerts,
        lambda result: write_investigation(
            settings, result, investigation_path(settings, result.alert_id)
        ),
        batch_day=day,
    )


def write_investigation(
    settings: Settings, investigation: Investigation, path: Path | None = None
) -> Path:
    """Keep an investigation at `path`; by default, with the case sets' investigations. The file
    is replaced whole, as the API may be reading it."""
    if path is None:
        name = f"{investigation.alert_id.replace(':', '_')}.json"
        path = settings.agent.investigations_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(investigation.model_dump_json(indent=1))
    os.replace(tmp, path)
    return path
