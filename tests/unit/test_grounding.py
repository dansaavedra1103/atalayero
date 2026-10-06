from collections.abc import Iterable
from datetime import date, datetime

from atalayero.agent.grounding import observed_ids, verify
from atalayero.schemas import CaseAlert, CaseReport, Evidence

ALERT = CaseAlert(
    alert_id="2022-09-05:001:A",
    account_key="001:A",
    day=date(2022, 9, 5),
    sources=("model",),
    score=0.5,
    rank=1,
    transaction_ids=(3,),
)
CUTOFF = datetime(2022, 9, 6)
WAREHOUSE = {
    1: (datetime(2022, 9, 5, 9), 6000.0),
    3: (datetime(2022, 9, 5, 11), 5600.0),
    6: (datetime(2022, 9, 6, 9), 6000.0),  # the day after
}


def lookup(ids: Iterable[int]) -> dict[int, tuple[datetime, float]]:
    return {i: WAREHOUSE[i] for i in ids if i in WAREHOUSE}


def report(evidence: list[tuple[int, float]], narrative: str = "", **fields: str) -> CaseReport:
    return CaseReport(
        alert_id=fields.get("alert_id", ALERT.alert_id),
        decision=fields.get("decision", "escalate"),
        typology=fields.get("typology", "cycle"),
        evidence=tuple(Evidence(transaction_id=i, amount_usd=a) for i, a in evidence),
        confidence=0.7,
        narrative=narrative,
    )


def check(r: CaseReport, observed: set[int] = frozenset({1, 3})) -> tuple[bool, int, list[str]]:
    g = verify(r, ALERT, CUTOFF, set(observed), lookup, tolerance=0.01)
    return g.grounded, g.hallucinated_ids, g.problems


def test_a_report_citing_what_it_was_shown_is_grounded() -> None:
    assert check(report([(1, 6000.0), (3, 5600.0)], "From #1 back in #3.")) == (True, 0, [])
    assert check(report([(1, 6030.0)]))[0]  # within the 1% tolerance


def test_cited_transactions_that_cannot_be_trusted_are_hallucinated() -> None:
    grounded, hallucinated, problems = check(
        report([(99, 1.0), (6, 6000.0), (1, 6000.0)]), observed={3}
    )

    assert (grounded, hallucinated) == (False, 3)
    assert problems == [
        "#99 does not exist",
        "#6 happened after the alert's day",
        "#1 was never shown by a tool",
    ]


def test_a_wrong_amount_is_a_problem_but_not_a_hallucination() -> None:
    assert check(report([(1, 600.0)])) == (
        False,
        0,
        ["#1 is cited at 600.00 USD, not 6000.00"],
    )


def test_the_narrative_mentions_only_cited_transactions() -> None:
    grounded, hallucinated, problems = check(report([(1, 6000.0)], "See #1, #3 and #77."))

    assert (grounded, hallucinated) == (False, 1)  # #77 does not exist; #3 exists, was shown
    assert problems == [
        "#3 is mentioned in the narrative but not cited as evidence",
        "#77 is mentioned in the narrative but not cited as evidence",
    ]


def test_an_escalation_needs_evidence_and_a_report_its_alert() -> None:
    assert check(report([]))[2] == ["an escalation must cite at least one transaction"]
    assert check(report([], decision="close", typology="none"))[0]
    assert check(report([(1, 6000.0)], alert_id="other"))[2] == [
        f"the report is about other, not {ALERT.alert_id}"
    ]


def test_no_report_is_not_grounded() -> None:
    g = verify(None, ALERT, CUTOFF, set(), lookup, tolerance=0.01)

    assert (g.grounded, g.problems) == (False, ["no valid report"])


def test_observed_ids_come_from_any_depth_of_a_tool_output() -> None:
    output = {
        "transactions": [{"transaction_id": 1}, {"transaction_id": 2}],
        "cycles": [{"transaction_ids": [3, 4], "path": ["A", "B"]}],
        "explained": [{"transaction_id": 5, "top_factors": [{"value": 6}]}],
    }

    assert observed_ids(output) == {1, 2, 3, 4, 5}
