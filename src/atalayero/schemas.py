"""Shared schemas: defined once, reused by streaming, API, agent and tests."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The laundering typologies of the source's patterns file.
TYPOLOGIES = (
    "fan_out",
    "fan_in",
    "cycle",
    "bipartite",
    "stack",
    "random",
    "scatter_gather",
    "gather_scatter",
)


class Transaction(BaseModel):
    """A transaction as the monitoring system sees it: no label (`extra="forbid"` keeps any
    ground-truth field from sneaking in)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: int
    transacted_at: datetime
    sender_account_key: str  # "bank:account"
    receiver_account_key: str
    amount_paid: Decimal
    payment_currency: str
    amount_paid_usd: float
    amount_received: Decimal
    receiving_currency: str
    amount_received_usd: float
    payment_format: str


class Alert(BaseModel):
    """A rule hit on an account, triggered by one transaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str  # "<rule_id>:<transaction_id>", so a replayed alert keeps its ID
    rule_id: str
    rule_version: str
    account_key: str
    triggered_at: datetime  # event time of the triggering transaction
    transaction_id: int
    value: float  # what the rule measured, e.g. 12 distinct counterparties
    evidence: tuple[int, ...]  # transactions in the window that count, most recent first


class CaseAlert(BaseModel):
    """An account-day handed to an investigator (ADR-0015): what the detectors saw, never the
    label. The investigation may only look at data before the end of `day`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str  # "<day>:<account_key>"
    account_key: str
    day: date
    sources: tuple[str, ...]  # the rules that alerted the account-day, and "model" if it was
    # within the model's daily budget
    score: float  # the model's highest transaction score of the account-day
    rank: int  # the account-day's rank by that score, among all account-days of the day
    transaction_ids: tuple[int, ...]  # what raised it: the rules' triggering transactions and
    # the account-day's top-scored ones


Decision = Literal["escalate", "close"]
# What an investigator can conclude: a typology, laundering without a clear one, or none.
ReportTypology = Literal[(*TYPOLOGIES, "unclassified", "none")]


class Evidence(BaseModel):
    """A transaction a report relies on, with the amount the report states for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: int
    amount_usd: float


class CaseReport(BaseModel):
    """An investigator's conclusion on one `CaseAlert` (design.md §4). Evidence is structured,
    so that every cited transaction and amount can be checked (ADR-0015)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str
    decision: Decision
    typology: ReportTypology
    evidence: tuple[Evidence, ...]
    confidence: float = Field(ge=0, le=1)
    narrative: str

    @model_validator(mode="after")
    def _consistent(self) -> "CaseReport":
        if (self.decision == "close") != (self.typology == "none"):
            raise ValueError("a report closes with typology 'none', and only then")
        return self
