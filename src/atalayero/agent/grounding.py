"""The grounding check of a case report (ADR-0018): deterministic, with no language model.

A report is grounded when every transaction it relies on exists, happened before the end of the
alert's day, was shown to the investigator by a tool, and is cited with its real amount; when its
narrative mentions no transaction it does not cite; and when an escalation cites at least one.
A cited transaction that does not exist, is past the cut-off, or was never shown is counted as
hallucinated.
"""

import re
from collections.abc import Callable, Iterable
from datetime import datetime

from pydantic import BaseModel

from atalayero.schemas import CaseAlert, CaseReport

Lookup = Callable[[Iterable[int]], dict[int, tuple[datetime, float]]]
# Transactions are referred to as #<id> in a narrative (the drafting prompt asks for it).
NARRATIVE_ID = re.compile(r"#(\d+)")


class Grounding(BaseModel):
    grounded: bool
    hallucinated_ids: int
    problems: list[str]


def observed_ids(output: object) -> set[int]:
    """The transaction IDs in a tool's output: under `transaction_id` and `transaction_ids`."""
    found: set[int] = set()
    if isinstance(output, dict):
        for key, value in output.items():
            if key == "transaction_id" and isinstance(value, int):
                found.add(value)
            elif key == "transaction_ids" and isinstance(value, list):
                found.update(v for v in value if isinstance(v, int))
            else:
                found |= observed_ids(value)
    elif isinstance(output, list):
        for value in output:
            found |= observed_ids(value)
    return found


def verify(
    report: CaseReport | None,
    alert: CaseAlert,
    cutoff: datetime,
    observed: set[int],
    lookup: Lookup,
    tolerance: float,
) -> Grounding:
    """Check `report` against the warehouse and against what the investigator was shown."""
    if report is None:
        return Grounding(grounded=False, hallucinated_ids=0, problems=["no valid report"])
    cited = {e.transaction_id for e in report.evidence}
    mentioned = {int(i) for i in NARRATIVE_ID.findall(report.narrative)}
    known = lookup(cited | mentioned)
    problems: list[str] = []
    hallucinated: set[int] = set()
    for evidence in report.evidence:
        tid = evidence.transaction_id
        if tid not in known:
            problems.append(f"#{tid} does not exist")
            hallucinated.add(tid)
            continue
        at, amount = known[tid]
        if at >= cutoff:
            problems.append(f"#{tid} happened after the alert's day")
            hallucinated.add(tid)
        elif tid not in observed:
            problems.append(f"#{tid} was never shown by a tool")
            hallucinated.add(tid)
        if abs(evidence.amount_usd - amount) > max(tolerance * abs(amount), 0.01):
            problems.append(f"#{tid} is cited at {evidence.amount_usd:.2f} USD, not {amount:.2f}")
    for tid in sorted(mentioned - cited):
        if tid not in known or tid not in observed:
            hallucinated.add(tid)
        problems.append(f"#{tid} is mentioned in the narrative but not cited as evidence")
    if report.decision == "escalate" and not report.evidence:
        problems.append("an escalation must cite at least one transaction")
    if report.alert_id != alert.alert_id:
        problems.append(f"the report is about {report.alert_id}, not {alert.alert_id}")
    return Grounding(grounded=not problems, hallucinated_ids=len(hallucinated), problems=problems)
