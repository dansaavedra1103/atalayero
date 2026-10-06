import asyncio
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from atalayero.agent.config import AgentConfig, load_agent_config
from atalayero.agent.graph import Investigation, Investigator, draft_schema
from atalayero.agent.llm import ChatReply, ToolCall
from atalayero.agent.tools import ToolBox
from atalayero.mcp_server.server import build_server
from atalayero.rules.schema import load_rules
from atalayero.schemas import CaseAlert, Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]

REPO = Path(__file__).parents[2]
A = CaseAlert(
    alert_id="2022-09-05:001:A",
    account_key="001:A",
    day=date(2022, 9, 5),
    sources=("R03",),
    score=0.4,
    rank=12,
    transaction_ids=(3,),
)


@pytest.fixture
def toolbox(make_tx: MakeTx, fct_transactions_db: WriteDb) -> ToolBox:
    """On 5 Sep, A sends 6,000 to B, B sends 5,800 to C and C pays 5,600 back to A (#3, which
    raised the alert). #6 happens the next day."""
    transactions = [
        make_tx(1, 0, sender="001:A", receiver="001:B"),
        make_tx(2, 60, sender="001:B", receiver="001:C", usd=5800.0),
        make_tx(3, 120, sender="001:C", receiver="001:A", usd=5600.0),
        make_tx(6, 24 * 60, sender="001:A", receiver="001:E"),
    ]
    db = fct_transactions_db(transactions)
    return ToolBox(Settings().model_copy(update={"duckdb_path": db}), [A])


class Script:
    """A chat model that answers from a script and records what it is asked."""

    def __init__(self, replies: list[ChatReply | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        schema: dict[str, Any] | None,
    ) -> ChatReply:
        self.calls.append({"messages": list(messages), "tools": tools, "schema": schema})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def say(text: str = "") -> ChatReply:
    return ChatReply(content=text, prompt_tokens=10, output_tokens=5)


def call(*names: str, **arguments: Any) -> ChatReply:  # noqa: ANN401
    return ChatReply(tool_calls=[ToolCall(name=n, arguments=arguments) for n in names])


def report(**changes: Any) -> ChatReply:  # noqa: ANN401
    fields = {
        "decision": "escalate",
        "typology": "cycle",
        "evidence": [
            {"transaction_id": 1, "amount_usd": 6000.0},
            {"transaction_id": 3, "amount_usd": 5600.0},
        ],
        "confidence": 0.8,
        "narrative": "Money left in #1 and came back through C in #3.",
    }
    return ChatReply(content=json.dumps({**fields, **changes}))


def investigate(toolbox: ToolBox, script: Script, **config: Any) -> Investigation:  # noqa: ANN401
    agent_config = load_agent_config(REPO / "config" / "agent.yaml").model_copy(update=config)

    async def run() -> Investigation:
        async with Client(build_server(toolbox)) as client:
            investigator = Investigator(
                agent_config,
                script,
                client,
                toolbox.lookup,
                toolbox.cutoff,
                load_rules(REPO / "config" / "rules"),
            )
            return await investigator.investigate(A)

    return asyncio.run(run())


def test_a_grounded_investigation(toolbox: ToolBox) -> None:
    script = Script([say("plan"), call("get_transactions", days=1), say("enough"), report()])

    result = investigate(toolbox, script)

    assert result.error is None
    assert result.report is not None and result.report.alert_id == A.alert_id
    assert (result.report.decision, result.report.typology) == ("escalate", "cycle")
    assert result.grounding is not None and result.grounding.grounded
    assert (result.steps, result.llm_calls, result.retries) == (1, 4, 0)
    assert result.tokens == 30  # three replies of 15 tokens; the scripted calls cost none
    roles = [m["role"] for m in result.transcript]
    assert roles == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    tool_output = json.loads(result.transcript[5]["content"])
    assert tool_output["account_key"] == "001:A"  # the alert ID was added to the call
    assert "R03 (short_cycle)" in result.transcript[0]["content"]  # rules come from config


def test_tools_are_shown_without_the_alert_id_and_the_draft_follows_a_schema(
    toolbox: ToolBox,
) -> None:
    script = Script(
        [say("plan"), say("enough"), report(evidence=[], decision="close", typology="none")]
    )

    investigate(toolbox, script)

    tools = script.calls[1]["tools"]
    assert {t["function"]["name"] for t in tools} == {
        "get_account_profile",
        "get_transactions",
        "get_graph_neighborhood",
        "explain_score",
        "search_typologies",
    }
    for tool in tools:
        assert "alert_id" not in tool["function"]["parameters"].get("properties", {})
    schema = script.calls[2]["schema"]
    assert schema == draft_schema()
    assert "alert_id" not in schema["properties"] and "$ref" not in json.dumps(schema)
    assert script.calls[0]["tools"] is None  # triage plans without tools


def test_an_alert_id_from_the_model_is_overridden(toolbox: ToolBox) -> None:
    script = Script(
        [say("plan"), call("get_transactions", alert_id="2022-09-09:001:Z"), say(), report()]
    )

    result = investigate(toolbox, script)

    assert json.loads(result.transcript[5]["content"])["account_key"] == "001:A"


def test_a_report_that_fails_verification_goes_back_to_investigation(toolbox: ToolBox) -> None:
    bad = report(evidence=[{"transaction_id": 99, "amount_usd": 10.0}], narrative="See #99.")
    script = Script(
        [say("plan"), say("done"), bad, call("get_transactions", days=1), say("ok"), report()]
    )

    result = investigate(toolbox, script)

    assert result.grounding is not None and result.grounding.grounded
    assert (result.retries, result.steps, result.llm_calls) == (1, 1, 6)
    feedback = [m["content"] for m in result.transcript if "did not pass" in m["content"]]
    assert len(feedback) == 1 and "#99 does not exist" in feedback[0]


def test_retries_are_bounded(toolbox: ToolBox) -> None:
    bad = report(evidence=[{"transaction_id": 99, "amount_usd": 10.0}], narrative="See #99.")
    script = Script([say("plan"), say(), bad, say(), bad])

    result = investigate(toolbox, script, max_grounding_retries=1)

    assert result.report is not None  # the last report is kept, marked as not grounded
    assert result.grounding is not None and not result.grounding.grounded
    assert (result.grounding.hallucinated_ids, result.retries) == (1, 1)


def test_the_tool_budget_is_enforced(toolbox: ToolBox) -> None:
    calls = call("get_transactions", "get_account_profile", "get_graph_neighborhood")
    script = Script([say("plan"), calls, report()])

    result = investigate(toolbox, script, max_steps=2)

    assert (result.steps, result.llm_calls) == (2, 3)
    assert result.transcript[-3]["content"] == "Not run: the tool budget is spent."
    assert result.grounding is not None and result.grounding.grounded


def test_an_invalid_report_is_not_grounded(toolbox: ToolBox) -> None:
    script = Script([say("plan"), say(), say("not JSON")])

    result = investigate(toolbox, script, max_grounding_retries=0)

    assert result.report is None and result.error is None
    assert result.grounding is not None and not result.grounding.grounded
    assert result.grounding.problems[0].startswith("the report is not valid")


def test_a_crash_is_a_result(toolbox: ToolBox) -> None:
    script = Script([say("plan"), RuntimeError("Ollama is down")])

    result = investigate(toolbox, script)

    assert result.error == "RuntimeError: Ollama is down"
    assert result.report is None and result.llm_calls == 1


def test_the_agent_config_is_versioned() -> None:
    config = load_agent_config(REPO / "config" / "agent.yaml")
    fields = config.model_dump()

    with pytest.raises(ValueError, match="add an entry with the reason"):
        AgentConfig.model_validate({**fields, "version": "9.9"})
