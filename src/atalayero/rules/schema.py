"""Minimal schema for the rule YAML files in `config/rules/` (phase 1: R02 and R04)."""

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atalayero.schemas import TYPOLOGIES

_VERSION = r"^\d+\.\d+$"  # quoted in YAML: an unquoted 1.10 would load as the float 1.1
_DURATION = re.compile(r"(?P<amount>\d+)(?P<unit>[mhd])")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: object) -> timedelta:
    """Parse "90m", "24h" or "2d"."""
    if isinstance(value, timedelta):
        return value
    match = _DURATION.fullmatch(str(value))
    if not match or int(match["amount"]) == 0:
        raise ValueError(f"expected a positive duration like '24h', '90m' or '2d', got {value!r}")
    return timedelta(**{_UNITS[match["unit"]]: int(match["amount"])})


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(pattern=_VERSION)
    date: date
    reason: str = Field(min_length=1)


class Threshold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_count: int = Field(ge=1)  # the metric must reach this value
    min_amount_usd: float = Field(default=0, ge=0)  # smaller transactions do not count


class Rule(BaseModel):
    """One rule. Its metric is computed per sender account over outgoing transactions inside a
    trailing window; self-transfers never count."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^R\d{2}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    typology: str | None = None
    owner: str = Field(min_length=1)
    version: str = Field(pattern=_VERSION)
    description: str = Field(min_length=1)
    metric: Literal["distinct_counterparties", "outgoing_transactions"]
    window: timedelta
    threshold: Threshold
    cooldown: timedelta  # at most one alert per account and rule within this span
    history: tuple[HistoryEntry, ...] = Field(min_length=1)

    @field_validator("window", "cooldown", mode="before")
    @classmethod
    def _duration(cls, value: object) -> timedelta:
        return parse_duration(value)

    @field_validator("typology")
    @classmethod
    def _known_typology(cls, value: str | None) -> str | None:
        if value is not None and value not in TYPOLOGIES:
            raise ValueError(f"unknown typology {value!r}")
        return value

    @model_validator(mode="after")
    def _history_ends_at_current_version(self) -> "Rule":
        if self.history[-1].version != self.version:
            raise ValueError(
                f"the last history entry is version {self.history[-1].version}, "
                f"but the rule is version {self.version}: add an entry with the reason"
            )
        return self


def load_rules(rules_dir: Path) -> list[Rule]:
    """Load and validate every `<ID>_<name>.yaml` in `rules_dir`."""
    rules = []
    for path in sorted(rules_dir.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text()))
        if path.stem != f"{rule.id}_{rule.name}":
            raise ValueError(f"{path.name}: file name must be {rule.id}_{rule.name}.yaml")
        rules.append(rule)
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate rule IDs in {rules_dir}: {ids}")
    return rules
