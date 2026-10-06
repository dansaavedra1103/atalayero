"""The local models, served by Ollama (ADR-0017): pulled over HTTP and pinned by digest, so that
embeddings and answers can be reproduced."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import ollama
from pydantic import BaseModel

from atalayero.settings import Settings

if TYPE_CHECKING:
    from atalayero.agent.config import AgentConfig

logger = logging.getLogger(__name__)


def installed_digest(settings: Settings, model: str) -> str:
    """The digest of a model as Ollama has it; fails if it is not pulled."""
    for entry in ollama.Client(host=settings.ollama.url).list().models:
        if entry.model == model:
            return entry.digest
    raise LookupError(f"{model} is not in Ollama: run `make llm`")


def pull_models(settings: Settings) -> dict[str, str]:
    """Pull every model of `ollama.models` and check it against its pinned digest. Returns the
    digests; a model pinned to None is pulled and its digest logged, to be pinned."""
    client = ollama.Client(host=settings.ollama.url)
    digests = {}
    for model, pinned in settings.ollama.models.items():
        logger.info("Pulling %s", model)
        last = None
        for progress in client.pull(model, stream=True):
            if progress.status != last:
                logger.info("%s: %s", model, progress.status)
                last = progress.status
        digest = installed_digest(settings, model)
        if pinned is None:
            logger.warning("%s is not pinned; pin it in config/settings.yaml: %s", model, digest)
        elif digest != pinned:
            raise ValueError(f"{model} is at {digest}, not at its pinned {pinned}")
        digests[model] = digest
    return digests


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class ChatReply(BaseModel):
    """What a chat model answered: text, tool calls, and what it cost."""

    content: str = ""
    tool_calls: list[ToolCall] = []
    prompt_tokens: int = 0
    output_tokens: int = 0


# messages, tools (or None), a JSON schema the answer must follow (or None) -> the reply
ChatModel = Callable[
    [list[dict[str, Any]], list[dict[str, Any]] | None, dict[str, Any] | None], ChatReply
]


def ollama_chat(settings: Settings, config: "AgentConfig") -> ChatModel:
    """The agent's language model on Ollama, with its configured options."""
    client = ollama.Client(host=settings.ollama.url)

    def chat(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> ChatReply:
        response = client.chat(
            model=config.model,
            messages=messages,
            tools=tools,
            format=schema,
            options=config.options,
            think=config.think,
        )
        message = response.message
        return ChatReply(
            content=message.content or "",
            tool_calls=[
                ToolCall(name=c.function.name, arguments=dict(c.function.arguments))
                for c in message.tool_calls or []
            ],
            prompt_tokens=response.prompt_eval_count or 0,
            output_tokens=response.eval_count or 0,
        )

    return chat


def check_pinned(settings: Settings, model: str) -> str:
    """The model's digest, if it is pinned and Ollama has exactly that version."""
    pinned = settings.ollama.models.get(model)
    if pinned is None:
        raise ValueError(f"{model} is not pinned in config/settings.yaml (ollama.models)")
    digest = installed_digest(settings, model)
    if digest != pinned:
        raise ValueError(f"{model} is at {digest}, not at its pinned {pinned}: run `make llm`")
    return digest
