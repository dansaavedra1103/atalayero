"""`config/agent.yaml`: the investigator's model, options, limits and prompts (ADR-0018)."""

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from atalayero.rules.schema import HistoryEntry

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS = ("investigate", "triage", "draft")


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(pattern=r"^\d+\.\d+$")
    model: str
    think: bool | Literal["low", "medium", "high"] | None = None
    options: dict[str, float | int]
    max_steps: int = Field(ge=1)
    max_grounding_retries: int = Field(ge=0)
    amount_tolerance: float = Field(ge=0)
    history: tuple[HistoryEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _versioned(self) -> "AgentConfig":
        if self.history[-1].version != self.version:
            raise ValueError(
                f"the last history entry is version {self.history[-1].version}, but the config "
                f"is version {self.version}: add an entry with the reason"
            )
        return self


def load_agent_config(path: Path) -> AgentConfig:
    return AgentConfig.model_validate(yaml.safe_load(path.read_text()))


def load_prompts() -> dict[str, str]:
    return {name: (PROMPTS_DIR / f"{name}.md").read_text() for name in PROMPTS}


def prompts_sha256() -> str:
    """A checksum of the prompts, recorded with every investigation."""
    digest = hashlib.sha256()
    for name in PROMPTS:
        digest.update((PROMPTS_DIR / f"{name}.md").read_bytes())
    return digest.hexdigest()
