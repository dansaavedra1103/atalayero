"""The investigator agent (ADR-0018): a LangGraph graph over the MCP tools.

    triage → investigate ⟲ → draft → ground → end
                  ↑                     │
                  └──── not grounded ───┘ (at most `max_grounding_retries` times)

- **triage** reads the alert and writes a plan, without tools.
- **investigate** calls tools through the MCP server, until the model stops or spends its
  `max_steps`.
- **draft** writes a `CaseReport` as JSON that follows its schema.
- **ground** verifies it, without a language model (`grounding.py`). A report that fails goes
  back to investigation with the problems found.

The alert ID never goes through the model: the tools are shown without it, and it is added to
every call, so an investigation cannot drift to another alert.
"""

import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from mcp import Client
from pydantic import BaseModel, ValidationError

from atalayero.agent.config import AgentConfig, load_prompts, prompts_sha256
from atalayero.agent.grounding import Grounding, Lookup, observed_ids, verify
from atalayero.agent.llm import ChatModel, ToolCall
from atalayero.rules.schema import Rule
from atalayero.schemas import CaseAlert, CaseReport

DRAFT_FIELDS = ("decision", "typology", "evidence", "confidence", "narrative")


def _inline(schema: dict[str, Any], defs: dict[str, Any]) -> Any:  # noqa: ANN401
    """A JSON schema with its `$ref`s replaced by their definitions."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            return _inline(defs[schema["$ref"].split("/")[-1]], defs)
        return {k: _inline(v, defs) for k, v in schema.items() if k != "$defs"}
    if isinstance(schema, list):
        return [_inline(v, defs) for v in schema]
    return schema


def draft_schema() -> dict[str, Any]:
    """The JSON schema a draft follows: a `CaseReport` without its alert ID, which the agent
    adds itself."""
    schema = CaseReport.model_json_schema()
    schema = _inline(schema, schema.get("$defs", {}))
    schema["properties"].pop("alert_id")
    schema["required"] = [f for f in schema["required"] if f != "alert_id"]
    return schema


class Investigation(BaseModel):
    """The outcome of one investigation, and what it took."""

    alert_id: str
    report: CaseReport | None
    grounding: Grounding | None
    steps: int  # tool calls
    llm_calls: int
    retries: int  # times the report went back to investigation
    tokens: int
    seconds: float
    config_version: str
    prompts_sha256: str
    error: str | None = None
    transcript: list[dict[str, Any]]


class State(TypedDict):
    messages: list[dict[str, Any]]
    steps: int
    llm_calls: int
    tokens: int
    observed: set[int]
    more: bool  # the last investigation turn called tools and the budget is not spent
    report: CaseReport | None
    draft_error: str | None
    grounding: Grounding | None
    retries: int
    finished: bool


def _tool_for_model(tool: Any) -> dict[str, Any]:  # noqa: ANN401
    """An MCP tool as Ollama takes it, without the alert ID."""
    parameters = json.loads(json.dumps(tool.input_schema))
    parameters.get("properties", {}).pop("alert_id", None)
    parameters["required"] = [r for r in parameters.get("required", []) if r != "alert_id"]
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": parameters,
        },
    }


class Investigator:
    def __init__(
        self,
        config: AgentConfig,
        chat: ChatModel,
        client: Client,
        lookup: Lookup,
        cutoff: Callable[[CaseAlert], datetime],
        rules: Sequence[Rule],
    ) -> None:
        self.config = config
        self.chat = chat
        self.client = client
        self.lookup = lookup
        self.cutoff = cutoff
        prompts = load_prompts()
        described = "\n".join(f"  - {r.id} ({r.name}): {r.description}" for r in rules)
        self.system = (
            prompts["investigate"]
            .replace("{rules}", described)
            .replace("{max_steps}", str(config.max_steps))
            .replace("{model_call_rank}", str(config.model_call_rank))
        )
        self.triage_prompt = prompts["triage"]
        self.draft_prompt = prompts["draft"]
        self.schema = draft_schema()
        self.prompts_sha256 = prompts_sha256()

    async def _call_tool(self, alert: CaseAlert, call: ToolCall) -> tuple[object, str]:
        """Run a tool for the alert; return its output and the text the model reads."""
        arguments = {k: v for k, v in call.arguments.items() if k != "alert_id"}
        result = await self.client.call_tool(call.name, {**arguments, "alert_id": alert.alert_id})
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content) or "the tool failed"
            return None, f"Error: {text}"
        return result.structured_content, json.dumps(result.structured_content)

    def _ask(self, state: State, messages: list[dict[str, Any]], **kwargs: Any) -> Any:  # noqa: ANN401
        reply = self.chat(messages, kwargs.get("tools"), kwargs.get("schema"))
        state["llm_calls"] += 1
        state["tokens"] += reply.prompt_tokens + reply.output_tokens
        return reply

    def _graph(self, alert: CaseAlert, tools: list[dict[str, Any]]) -> Any:  # noqa: ANN401
        config = self.config

        async def triage(state: State) -> State:
            state = State(**state)
            threshold = config.model_call_rank
            model_call = (
                f"escalate (rank {alert.rank}, within the top {threshold} of its day)"
                if alert.rank <= threshold
                else f"close (rank {alert.rank}, below the top {threshold} of its day)"
            )
            prompt = self.triage_prompt.replace("{alert}", alert.model_dump_json(indent=1)).replace(
                "{model_call}", model_call
            )
            messages = [*state["messages"], {"role": "user", "content": prompt}]
            reply = self._ask(state, messages)
            messages += [
                {"role": "assistant", "content": reply.content},
                {"role": "user", "content": "Now investigate, using the tools."},
            ]
            return {**state, "messages": messages}

        async def investigate(state: State) -> State:
            state = State(**state)
            if state["steps"] >= config.max_steps:
                return {**state, "more": False}
            reply = self._ask(state, state["messages"], tools=tools)
            turn: dict[str, Any] = {"role": "assistant", "content": reply.content}
            if reply.tool_calls:
                turn["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}}
                    for c in reply.tool_calls
                ]
            messages = [*state["messages"], turn]
            steps, observed = state["steps"], set(state["observed"])
            for call in reply.tool_calls:
                if steps >= config.max_steps:
                    text = "Not run: the tool budget is spent."
                else:
                    output, text = await self._call_tool(alert, call)
                    observed |= observed_ids(output)
                    steps += 1
                messages.append({"role": "tool", "tool_name": call.name, "content": text})
            more = bool(reply.tool_calls) and steps < config.max_steps
            return {
                **state,
                "messages": messages,
                "steps": steps,
                "observed": observed,
                "more": more,
            }

        async def draft(state: State) -> State:
            state = State(**state)
            messages = [*state["messages"], {"role": "user", "content": self.draft_prompt}]
            reply = self._ask(state, messages, schema=self.schema)
            messages.append({"role": "assistant", "content": reply.content})
            report, error = None, None
            try:
                fields = json.loads(reply.content)
                report = CaseReport(
                    alert_id=alert.alert_id, **{k: fields.get(k) for k in DRAFT_FIELDS}
                )
            except (ValueError, ValidationError, TypeError, AttributeError) as exc:
                error = f"the report is not valid: {exc}"
            return {**state, "messages": messages, "report": report, "draft_error": error}

        async def ground(state: State) -> State:
            state = State(**state)
            grounding = verify(
                state["report"],
                alert,
                self.cutoff(alert),
                state["observed"],
                self.lookup,
                config.amount_tolerance,
            )
            if state["draft_error"]:
                grounding.problems = [state["draft_error"]]
            if grounding.grounded or state["retries"] >= config.max_grounding_retries:
                return {**state, "grounding": grounding, "finished": True}
            feedback = (
                "Your report did not pass verification:\n"
                + "\n".join(f"- {p}" for p in grounding.problems)
                + "\nInvestigate further if you need to; you will then report again."
            )
            return {
                **state,
                "grounding": grounding,
                "retries": state["retries"] + 1,
                "messages": [*state["messages"], {"role": "user", "content": feedback}],
                "finished": False,
            }

        graph = StateGraph(State)
        graph.add_node("triage", triage)
        graph.add_node("investigate", investigate)
        graph.add_node("draft", draft)
        graph.add_node("ground", ground)
        graph.add_edge(START, "triage")
        graph.add_edge("triage", "investigate")
        graph.add_conditional_edges(
            "investigate", lambda s: "investigate" if s["more"] else "draft"
        )
        graph.add_edge("draft", "ground")
        graph.add_conditional_edges("ground", lambda s: END if s["finished"] else "investigate")
        return graph.compile()

    async def investigate(self, alert: CaseAlert) -> Investigation:
        """Investigate one alert. A failure is part of the result, not an exception."""
        listed = await self.client.list_tools()
        tools = [_tool_for_model(t) for t in listed.tools]
        state: State = {
            "messages": [{"role": "system", "content": self.system}],
            "steps": 0,
            "llm_calls": 0,
            "tokens": 0,
            "observed": set(alert.transaction_ids),  # the alert itself shows them
            "more": True,
            "report": None,
            "draft_error": None,
            "grounding": None,
            "retries": 0,
            "finished": False,
        }
        limit = (self.config.max_steps + 4) * (self.config.max_grounding_retries + 1) + 4
        start, error = time.monotonic(), None
        try:  # stream the states, so that a crash still leaves the last one to report
            graph = self._graph(alert, tools)
            initial = state
            async for update in graph.astream(
                initial, {"recursion_limit": limit}, stream_mode="values"
            ):
                state = update
        except Exception as exc:  # a crashed investigation is reported, not raised
            error = f"{type(exc).__name__}: {exc}"
        return Investigation(
            alert_id=alert.alert_id,
            report=state["report"] if error is None else None,
            grounding=state["grounding"] if error is None else None,
            steps=state["steps"],
            llm_calls=state["llm_calls"],
            retries=state["retries"],
            tokens=state["tokens"],
            seconds=time.monotonic() - start,
            config_version=self.config.version,
            prompts_sha256=self.prompts_sha256,
            error=error,
            transcript=state["messages"],
        )
