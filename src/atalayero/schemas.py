"""Shared schemas: defined once, reused by streaming, API, agent and tests."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

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
