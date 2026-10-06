"""Metrics of an investigator on a case set (ADR-0015, design.md §5), and the report that holds
them. Every group is reported apart; the primary metric is the mean of the groups' decision
accuracies, so a detector cannot win by deciding the same way on every case."""

import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from atalayero.evals.golden import GROUPS, CaseAnswer, CaseSet, Group
from atalayero.schemas import TYPOLOGIES, CaseReport


class CaseResult(BaseModel):
    """What a detector produced on one case, and what it cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str
    report: CaseReport | None  # None when no valid report came out
    grounded: bool | None = None  # None for a detector that cites nothing to check
    hallucinated_ids: int | None = None  # cited transactions that do not exist or are out of reach
    steps: int | None = None
    seconds: float | None = None
    error: str | None = None


class GroupMetrics(BaseModel):
    cases: int
    decision_accuracy: float
    typology_accuracy: float | None = None  # patterned: the typology is one of the accepted ones
    specific_typology_share: float | None = None  # untyped: a specific typology was claimed


class AgentEvalReport(BaseModel):
    """The evaluation of one detector on one case set: written to `evals/reports/`, versioned,
    and validated by CI."""

    detector: str
    case_set: CaseSet
    generated_at: datetime
    cases_sha256: str
    config: dict[str, str | int | float]
    primary: float  # mean decision accuracy over the three groups
    groups: dict[Group, GroupMetrics]
    failures: int  # cases without a valid report: they count as wrong
    grounded_share: float | None
    hallucinated_ids_per_report: float | None
    median_steps: float | None
    median_seconds: float | None
    results: list[CaseResult]

    def table(self) -> str:
        """The per-group results as a Markdown table."""
        lines = [
            "| Group | Cases | Decision | Typology | Specific typology claimed |",
            "| --- | --- | --- | --- | --- |",
        ]
        for group, m in self.groups.items():
            typology = "—" if m.typology_accuracy is None else f"{m.typology_accuracy:.0%}"
            specific = (
                "—" if m.specific_typology_share is None else f"{m.specific_typology_share:.0%}"
            )
            lines.append(
                f"| {group} | {m.cases} | {m.decision_accuracy:.0%} | {typology} | {specific} |"
            )
        lines.append(f"\nPrimary (mean decision accuracy): {self.primary:.1%}")
        return "\n".join(lines)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _optional_median(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return statistics.median(known) if known else None


def group_metrics(
    results: Sequence[CaseResult], answers: Mapping[str, CaseAnswer]
) -> dict[Group, GroupMetrics]:
    """The metrics of each group; every case needs exactly one result."""
    by_id = {r.alert_id: r for r in results}
    if len(by_id) != len(results) or set(by_id) != set(answers):
        raise ValueError("results must cover every case of the set exactly once")
    groups: dict[Group, GroupMetrics] = {}
    for group in GROUPS:
        cases = [
            (answers[i], by_id[i].report) for i in sorted(answers) if answers[i].group == group
        ]
        metrics = GroupMetrics(
            cases=len(cases),
            decision_accuracy=_mean([r is not None and r.decision == a.decision for a, r in cases]),
        )
        if group == "patterned":
            metrics.typology_accuracy = _mean(
                [r is not None and r.typology in a.typologies for a, r in cases]
            )
        if group == "untyped":
            metrics.specific_typology_share = _mean(
                [r is not None and r.typology in TYPOLOGIES for _, r in cases]
            )
        groups[group] = metrics
    return groups


def primary(groups: Mapping[Group, GroupMetrics]) -> float:
    """The mean decision accuracy over the groups that have cases."""
    return _mean([m.decision_accuracy for m in groups.values() if m.cases])


def summarize(
    results: Sequence[CaseResult],
    answers: Mapping[str, CaseAnswer],
    detector: str,
    case_set: CaseSet,
    cases_sha256: str,
    config: Mapping[str, str | int | float],
    generated_at: datetime,
) -> AgentEvalReport:
    """Score `results` against `answers` into a report."""
    groups = group_metrics(results, answers)
    grounded = [r.grounded for r in results if r.grounded is not None]
    hallucinated = [r.hallucinated_ids for r in results if r.hallucinated_ids is not None]
    return AgentEvalReport(
        detector=detector,
        case_set=case_set,
        generated_at=generated_at,
        cases_sha256=cases_sha256,
        config=dict(config),
        primary=primary(groups),
        groups=groups,
        failures=sum(r.report is None for r in results),
        grounded_share=_mean(grounded) if grounded else None,
        hallucinated_ids_per_report=_mean(hallucinated) if hallucinated else None,
        median_steps=_optional_median([r.steps for r in results]),
        median_seconds=_optional_median([r.seconds for r in results]),
        results=list(results),
    )
