from collections.abc import Callable

import pytest

from atalayero.rules.online import MAX_EVIDENCE, OnlineEvaluator, OnlineRule
from atalayero.rules.schema import Rule
from atalayero.schemas import Alert, Transaction

MakeTx = Callable[..., Transaction]

DAY = 24 * 60  # minutes


def _rule(metric: str = "distinct_counterparties", min_count: int = 3, **overrides: str) -> Rule:
    return Rule.model_validate(
        {
            "id": "R99",
            "name": "test_rule",
            "owner": "tests",
            "version": "1.0",
            "description": "A rule for tests",
            "metric": metric,
            "window": overrides.get("window", "24h"),
            "threshold": {"min_count": min_count, "min_amount_usd": 5000},
            "cooldown": overrides.get("cooldown", "24h"),
            "history": [{"version": "1.0", "date": "2026-10-04", "reason": "Initial version"}],
        }
    )


def _alerts(rule: Rule, transactions: list[Transaction]) -> list[Alert]:
    evaluator = OnlineEvaluator([rule])
    return [alert for tx in transactions for alert in evaluator.observe(tx)]


def test_fires_when_the_threshold_is_reached(make_tx: MakeTx) -> None:
    txs = [make_tx(1, 0, receiver="B"), make_tx(2, 10, receiver="C"), make_tx(3, 20, receiver="D")]

    assert _alerts(_rule(), txs[:2]) == []
    [alert] = _alerts(_rule(), txs)
    assert alert == Alert(
        alert_id="R99:3",
        rule_id="R99",
        rule_version="1.0",
        account_key="001:A",
        triggered_at=txs[2].transacted_at,
        transaction_id=3,
        value=3,
        evidence=(3, 2, 1),
    )


def test_counterparties_count_once_but_transactions_count_each(make_tx: MakeTx) -> None:
    txs = [make_tx(1, 0, receiver="B"), make_tx(2, 10, receiver="B"), make_tx(3, 20, receiver="C")]

    assert _alerts(_rule("distinct_counterparties"), txs) == []
    assert [a.value for a in _alerts(_rule("outgoing_transactions"), txs)] == [3]


def test_small_amounts_and_self_transfers_do_not_count(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, receiver="B", usd=5000),  # exactly the minimum: counts
        make_tx(2, 10, receiver="C", usd=4999.99),
        make_tx(3, 20, receiver="001:A"),  # to itself
        make_tx(4, 30, receiver="D"),
    ]

    assert _alerts(_rule(), txs) == []
    assert [a.transaction_id for a in _alerts(_rule(), [*txs, make_tx(5, 40, receiver="E")])] == [5]


def test_window_includes_its_start(make_tx: MakeTx) -> None:
    first_two = [make_tx(1, 0, receiver="B"), make_tx(2, 10, receiver="C")]

    assert len(_alerts(_rule(), [*first_two, make_tx(3, DAY, receiver="D")])) == 1
    assert _alerts(_rule(), [*first_two, make_tx(3, DAY + 1, receiver="D")]) == []


def test_cooldown_limits_alerts_per_account(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, receiver="B"),
        make_tx(2, 10, receiver="C"),
        make_tx(3, 20, receiver="D"),  # alert
        make_tx(4, 30, receiver="E"),  # still above the threshold, but cooling down
        make_tx(5, 20 + DAY, receiver="F"),  # cooldown over; D, E, F in the window
    ]

    assert [a.transaction_id for a in _alerts(_rule(), txs)] == [3, 5]


def test_accounts_are_evaluated_apart(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, receiver="B"),
        make_tx(2, 10, sender="001:X", receiver="C"),
        make_tx(3, 20, receiver="D"),
    ]

    assert _alerts(_rule(), txs) == []


def test_evidence_is_capped_most_recent_first(make_tx: MakeTx) -> None:
    n = MAX_EVIDENCE + 20
    txs = [make_tx(i, i / 10, receiver="B") for i in range(1, n + 1)]

    [alert] = _alerts(_rule("outgoing_transactions", min_count=n, window="1h"), txs)

    assert alert.value == n
    assert alert.evidence == tuple(range(n, n - MAX_EVIDENCE, -1))


def test_state_forgets_accounts_whose_window_and_cooldown_ran_out(make_tx: MakeTx) -> None:
    rule = OnlineRule(_rule(min_count=2, cooldown="2d"))
    alerts = [rule.observe(make_tx(i, i, sender=f"001:S{i}", receiver="B")) for i in range(50)]
    alerts.append(rule.observe(make_tx(100, 200, sender="001:S0", receiver="B")))
    alerts.append(rule.observe(make_tx(101, 210, sender="001:S0", receiver="C")))  # alert

    later = rule.observe(make_tx(102, 3 * DAY, sender="001:S1", receiver="B"))

    assert [a.transaction_id for a in alerts if a] == [101]
    assert later is None
    assert (len(rule._windows), len(rule._last_alert)) == (1, 0)


@pytest.mark.parametrize(
    ("first", "second"),
    [((2, 10), (3, 5)), ((2, 10), (1, 10)), ((2, 10), (2, 10))],
    ids=["earlier time", "same time, lower id", "duplicate"],
)
def test_input_out_of_event_time_order_fails(
    make_tx: MakeTx, first: tuple[int, int], second: tuple[int, int]
) -> None:
    evaluator = OnlineEvaluator([_rule()])
    evaluator.observe(make_tx(*first))

    with pytest.raises(ValueError, match="out of event-time order"):
        evaluator.observe(make_tx(*second))
