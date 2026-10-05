import random
from collections.abc import Callable
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from atalayero.rules.online import MAX_EVIDENCE, CycleRule, OnlineEvaluator, OnlineRule
from atalayero.rules.schema import Rule, load_rules
from atalayero.schemas import Alert, Transaction

MakeTx = Callable[..., Transaction]

DAY = 24 * 60  # minutes


REPO_ROOT = Path(__file__).parents[2]


def _rule(metric: str = "distinct_counterparties", min_count: int = 3, **overrides: str) -> Rule:
    return Rule.model_validate(
        {
            "id": "R99",
            "name": "test_rule",
            "owner": "tests",
            "version": "1.0",
            "description": "A rule for tests",
            "metric": metric,
            "direction": overrides.get("direction", "out"),
            "window": overrides.get("window", "24h"),
            "threshold": {"min_count": min_count, "min_amount_usd": 5000},
            "cooldown": overrides.get("cooldown", "24h"),
            "history": [{"version": "1.0", "date": "2026-10-04", "reason": "Initial version"}],
        }
    )


def _cycle_rule(max_hops: int = 3, window: str = "24h", cooldown: str = "24h") -> Rule:
    return _rule().model_copy(
        update={
            "metric": "short_cycle",
            "window": timedelta(hours=int(window[:-1])),
            "cooldown": timedelta(hours=int(cooldown[:-1])),
            "threshold": _rule().threshold.model_copy(
                update={"min_count": None, "max_hops": max_hops}
            ),
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


def test_incoming_direction_counts_senders_per_receiver(make_tx: MakeTx) -> None:
    rule = _rule(direction="in")
    txs = [
        make_tx(1, 0, sender="001:B", receiver="001:R"),
        make_tx(2, 10, sender="001:C", receiver="001:R"),
        make_tx(3, 15, sender="001:C", receiver="001:R"),  # same sender again
        make_tx(4, 20, sender="001:D", receiver="001:Q"),  # another receiver
        make_tx(5, 30, sender="001:E", receiver="001:R"),
    ]

    [alert] = _alerts(rule, txs)

    assert (alert.account_key, alert.transaction_id, alert.value) == ("001:R", 5, 3)
    assert alert.evidence == (5, 3, 2, 1)


def test_incoming_direction_uses_the_received_amount(make_tx: MakeTx) -> None:
    rule = _rule(direction="in")
    txs = [
        make_tx(i, i, sender=f"001:S{i}", receiver="001:R").model_copy(
            update={"amount_received_usd": 4999.0}
        )
        for i in range(1, 4)
    ]

    assert _alerts(rule, txs) == []


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


# short_cycle


def test_cycle_fires_on_the_closing_transaction(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, sender="A", receiver="B"),
        make_tx(2, 60, sender="B", receiver="C"),
        make_tx(3, 120, sender="C", receiver="A"),
    ]

    [alert] = _alerts(_cycle_rule(), txs)

    assert alert == Alert(
        alert_id="R99:3",
        rule_id="R99",
        rule_version="1.0",
        account_key="A",  # where the money came back
        triggered_at=txs[2].transacted_at,
        transaction_id=3,
        value=3,
        evidence=(3, 2, 1),
    )


def test_reciprocal_payment_is_a_two_hop_cycle(make_tx: MakeTx) -> None:
    txs = [make_tx(1, 0, sender="A", receiver="B"), make_tx(2, 5, sender="B", receiver="A")]

    assert [(a.account_key, a.value) for a in _alerts(_cycle_rule(max_hops=2), txs)] == [("A", 2)]


def test_cycle_legs_must_follow_each_other_in_time(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, sender="B", receiver="C"),  # leaves B before the money reaches it
        make_tx(2, 60, sender="A", receiver="B"),
        make_tx(3, 120, sender="C", receiver="A"),
    ]

    assert _alerts(_cycle_rule(), txs) == []


def test_cycle_legs_may_share_a_minute(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, sender="A", receiver="B"),
        make_tx(2, 0, sender="B", receiver="C"),
        make_tx(3, 0, sender="C", receiver="A"),
    ]

    assert [a.value for a in _alerts(_cycle_rule(), txs)] == [3]


@pytest.mark.parametrize(("max_hops", "fires"), [(3, False), (4, True)])
def test_cycle_length_is_capped(make_tx: MakeTx, max_hops: int, fires: bool) -> None:
    accounts = ["A", "B", "C", "D", "A"]
    txs = [make_tx(i, 10 * i, sender=s, receiver=r) for i, (s, r) in enumerate(pairwise(accounts))]

    assert bool(_alerts(_cycle_rule(max_hops=max_hops), txs)) is fires


@pytest.mark.parametrize(("closing_minute", "fires"), [(DAY, True), (DAY + 1, False)])
def test_cycle_must_fit_the_window(make_tx: MakeTx, closing_minute: int, fires: bool) -> None:
    txs = [
        make_tx(1, 0, sender="A", receiver="B"),
        make_tx(2, 60, sender="B", receiver="C"),
        make_tx(3, closing_minute, sender="C", receiver="A"),
    ]

    assert bool(_alerts(_cycle_rule(), txs)) is fires


def test_small_amounts_break_a_cycle(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, sender="A", receiver="B"),
        make_tx(2, 60, sender="B", receiver="C", usd=4999),
        make_tx(3, 120, sender="C", receiver="A"),
    ]

    assert _alerts(_cycle_rule(), txs) == []


def test_cycle_cooldown_is_per_origin_account(make_tx: MakeTx) -> None:
    txs = [
        make_tx(1, 0, sender="A", receiver="B"),
        make_tx(2, 10, sender="B", receiver="A"),  # alert on A
        make_tx(3, 20, sender="A", receiver="C"),
        make_tx(4, 30, sender="C", receiver="A"),  # A cooling down
        make_tx(5, 40, sender="C", receiver="B"),
        make_tx(6, 50, sender="B", receiver="C"),  # alert on C: C→B→C
    ]

    alerts = _alerts(_cycle_rule(max_hops=2), txs)

    assert [(a.transaction_id, a.account_key) for a in alerts] == [(2, "A"), (6, "C")]


def test_cycle_state_follows_the_window(make_tx: MakeTx) -> None:
    rule = CycleRule(_cycle_rule())
    for i in range(20):
        rule.observe(make_tx(i, i, sender=f"S{i}", receiver="B"))

    rule.observe(make_tx(100, 2 * DAY, sender="X", receiver="Y"))

    assert (len(rule._legs), set(rule._out), set(rule._in)) == (1, {"X"}, {"Y"})


def test_repository_r03_fires_on_the_fixture_cycle(make_tx: MakeTx) -> None:
    """The CYCLE attempt of tests/fixtures/hi_small_patterns_sample.txt: three hops in about
    three and a half days, 15,717-19,035 per leg."""
    [r03] = [r for r in load_rules(REPO_ROOT / "config" / "rules") if r.id == "R03"]
    start = 3 * 60 + 32  # 2022-09-05 12:32, minutes after T0
    txs = [
        make_tx(1, start, sender="021575:800EC1750", receiver="003335:8015A6530", usd=17_000),
        make_tx(2, start + 4730, sender="003335:8015A6530", receiver="022828:8011150C0"),
        make_tx(3, start + 5325, sender="022828:8011150C0", receiver="021575:800EC1750"),
    ]

    [alert] = _alerts(r03, txs)

    assert (alert.account_key, alert.value, alert.evidence) == ("021575:800EC1750", 3, (3, 2, 1))


def _brute_force_chain(
    legs: list[Transaction], origin: str, target: str, now: Transaction, rule: Rule
) -> int | None:
    """Length of the shortest time-ordered chain origin→…→target among `legs`, by trying every
    path; None if there is none."""
    start = now.transacted_at - rule.window
    usable = [
        leg
        for leg in legs
        if leg.transacted_at >= start
        and leg.sender_account_key != leg.receiver_account_key
        and leg.amount_paid_usd >= rule.threshold.min_amount_usd
    ]
    best: int | None = None

    def walk(account: str, arrival: object, used: int, seen: frozenset[str]) -> None:
        nonlocal best
        if account == target:
            best = used if best is None else min(best, used)
            return
        if used == rule.threshold.max_hops - 1:
            return
        for leg in usable:
            if (
                leg.sender_account_key == account
                and leg.receiver_account_key not in seen
                and (arrival is None or leg.transacted_at >= arrival)
            ):
                walk(leg.receiver_account_key, leg.transacted_at, used + 1, seen | {account})

    walk(origin, None, 0, frozenset())
    return best


@pytest.mark.parametrize("seed", range(30))
def test_cycle_search_matches_brute_force(make_tx: MakeTx, seed: int) -> None:
    rng = random.Random(seed)
    rule = _cycle_rule(max_hops=rng.choice([2, 3, 4, 5]), window="3h", cooldown="1h")
    accounts = [f"00{i}:X" for i in range(rng.randint(3, 7))]
    txs = [
        make_tx(
            i,
            minute,
            sender=rng.choice(accounts),
            receiver=rng.choice(accounts),
            usd=rng.choice([1000, 6000, 9000]),
        )
        for i, minute in enumerate(sorted(rng.randint(0, 600) for _ in range(60)))
    ]
    evaluator = CycleRule(rule)
    by_id = {tx.transaction_id: tx for tx in txs}

    last_alert: dict[str, object] = {}
    for i, tx in enumerate(txs):
        alert = evaluator.observe(tx)
        origin, target = tx.receiver_account_key, tx.sender_account_key
        expected = None
        cooling = origin in last_alert and tx.transacted_at - last_alert[origin] < rule.cooldown
        if origin != target and tx.amount_paid_usd >= 5000 and not cooling:
            expected = _brute_force_chain(txs[:i], origin, target, tx, rule)
        assert (alert and alert.value) == (expected and expected + 1), f"transaction {i}"
        if alert:
            last_alert[origin] = tx.transacted_at
            chain = [by_id[t] for t in reversed(alert.evidence)]
            assert chain[0].sender_account_key == chain[-1].receiver_account_key == origin
            assert all(
                a.receiver_account_key == b.sender_account_key
                and a.transacted_at <= b.transacted_at
                for a, b in pairwise(chain)
            )
