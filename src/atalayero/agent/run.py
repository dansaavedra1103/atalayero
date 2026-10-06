"""Run the investigator on alerts, with its MCP server as a subprocess over stdio (ADR-0018)."""

import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from mcp import Client, StdioServerParameters

from atalayero.agent.config import load_agent_config
from atalayero.agent.graph import Investigation, Investigator
from atalayero.agent.llm import check_pinned, ollama_chat
from atalayero.agent.tools import ToolBox
from atalayero.rules.schema import load_rules
from atalayero.schemas import CaseAlert
from atalayero.settings import Settings

logger = logging.getLogger(__name__)


async def investigate_alerts(
    settings: Settings,
    alerts: Sequence[CaseAlert],
    on_result: Callable[[Investigation], None] | None = None,
) -> list[Investigation]:
    """Investigate `alerts` one after the other, with the pinned model of `config/agent.yaml`."""
    config = load_agent_config(settings.agent.config_path)
    check_pinned(settings, config.model)
    toolbox = ToolBox(settings, alerts)  # the grounding check's own view of the warehouse
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "atalayero.mcp_server"], env=dict(os.environ)
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


def write_investigation(settings: Settings, investigation: Investigation) -> Path:
    directory = settings.agent.investigations_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{investigation.alert_id.replace(':', '_')}.json"
    path.write_text(investigation.model_dump_json(indent=1))
    return path
