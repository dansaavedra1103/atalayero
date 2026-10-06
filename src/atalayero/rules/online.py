"""Online evaluation of the rules over a transaction stream, in event-time order.

Each rule keeps the transactions inside its trailing window (`transacted_at >= t - window`, both
ends included). Only transactions already seen count, so the evaluation cannot look ahead in time.
"""

from collections import Counter, deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import islice

from atalayero.rules.schema import Rule
from atalayero.schemas import Alert, Transaction

MAX_EVIDENCE = 100  # transaction IDs kept in an alert; `value` holds the full measure


class _Window:
    __slots__ = ("counterparties", "entries")

    def __init__(self) -> None:
        self.entries: deque[tuple[datetime, str, int]] = deque()  # (time, counterparty, id)
        self.counterparties: Counter[str] = Counter()


class OnlineRule:
    """Count metrics (`distinct_counterparties`, `outgoing_transactions`), per account."""

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self._windows: dict[str, _Window] = {}
        self._last_alert: dict[str, datetime] = {}
        self._next_sweep: datetime | None = None

    def _sweep(self, now: datetime) -> None:
        """Forget accounts whose window and cooldown have both run out, so the state follows
        recent activity rather than every account ever seen."""
        start = now - self.rule.window
        self._windows = {a: w for a, w in self._windows.items() if w.entries[-1][0] >= start}
        self._last_alert = {
            a: t for a, t in self._last_alert.items() if now - t < self.rule.cooldown
        }

    def observe(self, tx: Transaction) -> Alert | None:
        rule = self.rule
        if rule.direction == "out":
            account, counterparty = tx.sender_account_key, tx.receiver_account_key
            amount_usd = tx.amount_paid_usd
        else:
            account, counterparty = tx.receiver_account_key, tx.sender_account_key
            amount_usd = tx.amount_received_usd
        if self._next_sweep is None or tx.transacted_at >= self._next_sweep:
            self._sweep(tx.transacted_at)
            self._next_sweep = tx.transacted_at + rule.window
        if account == counterparty or amount_usd < rule.threshold.min_amount_usd:
            return None

        window = self._windows.setdefault(account, _Window())
        window.entries.append((tx.transacted_at, counterparty, tx.transaction_id))
        window.counterparties[counterparty] += 1
        start = tx.transacted_at - rule.window
        while window.entries[0][0] < start:
            _, gone, _ = window.entries.popleft()
            window.counterparties[gone] -= 1
            if not window.counterparties[gone]:
                del window.counterparties[gone]

        if rule.metric == "distinct_counterparties":
            value = len(window.counterparties)
        else:
            value = len(window.entries)
        if value < rule.threshold.min_count:
            return None
        last = self._last_alert.get(account)
        if last is not None and tx.transacted_at - last < rule.cooldown:
            return None

        self._last_alert[account] = tx.transacted_at
        return Alert(
            alert_id=f"{rule.id}:{tx.transaction_id}",
            rule_id=rule.id,
            rule_version=rule.version,
            account_key=account,
            triggered_at=tx.transacted_at,
            transaction_id=tx.transaction_id,
            value=value,
            evidence=tuple(i for _, _, i in islice(reversed(window.entries), MAX_EVIDENCE)),
        )


_Leg = tuple[datetime, int, float]  # (time, transaction_id, US Dollar)
_Legs = dict[str, dict[str, deque[_Leg]]]  # account -> counterparty -> legs, oldest first
_Label = tuple[datetime, int, str]  # (time, transaction_id, neighbour one level down)


class RecentLegs:
    """The transactions of a trailing window, indexed by sender and by receiver, with a search for
    chains between two accounts in time order. Shared by the cycle rule (R03) and the motif
    features (ADR-0011). Self-transfers are never added."""

    def __init__(self, window: timedelta) -> None:
        self.window = window
        self._legs: deque[tuple[datetime, str, str, int]] = deque()  # in the window, by time
        self.out: _Legs = {}  # sender -> receiver -> legs
        self.into: _Legs = {}  # receiver -> sender -> legs

    def __len__(self) -> int:
        return len(self._legs)

    def expire(self, now: datetime) -> None:
        """Forget the legs that left the window `[now - window, now]`."""
        start = now - self.window
        while self._legs and self._legs[0][0] < start:
            _, sender, receiver, _ = self._legs.popleft()
            for index, account, counterparty in (
                (self.out, sender, receiver),
                (self.into, receiver, sender),
            ):
                legs = index[account][counterparty]
                legs.popleft()
                if not legs:
                    del index[account][counterparty]
                    if not index[account]:
                        del index[account]

    def add(
        self, time: datetime, sender: str, receiver: str, transaction_id: int, usd: float = 0.0
    ) -> None:
        """Add a leg; legs arrive in time order."""
        if sender == receiver:
            return
        leg = (time, transaction_id, usd)
        self._legs.append((time, sender, receiver, transaction_id))
        self.out.setdefault(sender, {}).setdefault(receiver, deque()).append(leg)
        self.into.setdefault(receiver, {}).setdefault(sender, deque()).append(leg)

    def find_chain(
        self,
        origin: str,
        target: str,
        now: datetime,
        max_legs: int,
        usd_range: tuple[float, float] | None = None,
    ) -> list[int] | None:
        """Transaction IDs of a shortest chain origin→…→target of at most `max_legs` legs inside
        the window, each leg at or after the previous one and, if `usd_range` is given, with an
        amount inside it; or None.

        The search runs from both ends, forward from `origin` over outgoing legs and backward from
        `target` over incoming ones, and always expands the cheaper side. Hubs send to thousands
        of accounts but receive from few, so they only cost much when the chain really goes
        through them. Level k of a side holds the accounts whose label improved with k legs.
        Forward labels are the earliest arrival from `origin`; backward labels are the latest
        departure that still reaches `target` by `now`. The sides meet at an account reached no
        later than it can leave.
        """
        start = now - self.window
        forward: list[dict[str, _Label]] = [{origin: (start, -1, "")}]
        backward: list[dict[str, _Label]] = [{target: (now, -1, "")}]
        best_forward = {origin: (start, 0)}
        best_backward = {target: (now, 0)}
        for _ in range(max_legs):
            cost_forward = sum(len(self.out.get(a, ())) for a in forward[-1])
            cost_backward = sum(len(self.into.get(a, ())) for a in backward[-1])
            if not cost_forward and not cost_backward:
                return None
            go_forward = not cost_backward or 0 < cost_forward <= cost_backward
            if go_forward:
                level = self._expand(
                    forward[-1], self.out, best_forward, len(forward), True, usd_range
                )
                forward.append(level)
            else:
                level = self._expand(
                    backward[-1], self.into, best_backward, len(backward), False, usd_range
                )
                backward.append(level)
            for account, (time, _, _) in level.items():
                if go_forward and account in best_backward:
                    departure, b = best_backward[account]
                    if time <= departure:
                        return self._chain(forward, backward, account, len(forward) - 1, b)
                elif not go_forward and account in best_forward:
                    arrival, f = best_forward[account]
                    if arrival <= time:
                        return self._chain(forward, backward, account, f, len(backward) - 1)
        return None

    @staticmethod
    def _expand(
        frontier: dict[str, _Label],
        index: _Legs,
        best: dict[str, tuple[datetime, int]],
        depth: int,
        later: bool,
        usd_range: tuple[float, float] | None,
    ) -> dict[str, _Label]:
        """Level `depth`: one more leg from every account in the frontier. Forward (`later`)
        takes the earliest leg at or after the arrival, backward the latest leg at or before the
        departure. Keeps only accounts whose label improves."""
        low, high = usd_range or (float("-inf"), float("inf"))
        level: dict[str, _Label] = {}
        for account, (time, _, _) in frontier.items():
            for other, legs in index.get(account, {}).items():
                if later:
                    leg = next((g for g in legs if g[0] >= time and low <= g[2] <= high), None)
                else:
                    leg = next(
                        (g for g in reversed(legs) if g[0] <= time and low <= g[2] <= high), None
                    )
                if leg is None:
                    continue
                current = level.get(other) or best.get(other)
                if current is None or (leg[0] < current[0] if later else leg[0] > current[0]):
                    level[other] = (leg[0], leg[1], account)
        for account, (time, _, _) in level.items():
            best[account] = (time, depth)
        return level

    @staticmethod
    def _chain(
        forward: list[dict[str, _Label]],
        backward: list[dict[str, _Label]],
        meeting: str,
        f: int,
        b: int,
    ) -> list[int]:
        chain = []
        account = meeting
        for k in range(f, 0, -1):
            _, transaction_id, account = forward[k][account]
            chain.append(transaction_id)
        chain.reverse()
        account = meeting
        for k in range(b, 0, -1):
            _, transaction_id, account = backward[k][account]
            chain.append(transaction_id)
        return chain


class CycleRule:
    """`short_cycle`: on each transaction C→A, look for an earlier chain A→…→C of at most
    `max_hops - 1` legs inside the window, each leg at or after the previous one. Money that left
    A has come back to it, so A gets the alert."""

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self.legs = RecentLegs(rule.window)
        self._last_alert: dict[str, datetime] = {}
        self._next_sweep: datetime | None = None

    def _expire(self, now: datetime) -> None:
        self.legs.expire(now)
        if self._next_sweep is None or now >= self._next_sweep:
            self._last_alert = {
                a: t for a, t in self._last_alert.items() if now - t < self.rule.cooldown
            }
            self._next_sweep = now + self.rule.cooldown

    def observe(self, tx: Transaction) -> Alert | None:
        rule, now = self.rule, tx.transacted_at
        self._expire(now)
        sender, receiver = tx.sender_account_key, tx.receiver_account_key
        if sender == receiver or tx.amount_paid_usd < rule.threshold.min_amount_usd:
            return None

        alert = None
        last = self._last_alert.get(receiver)
        if last is None or now - last >= rule.cooldown:
            chain = self.legs.find_chain(receiver, sender, now, rule.threshold.max_hops - 1)
            if chain is not None:
                self._last_alert[receiver] = now
                alert = Alert(
                    alert_id=f"{rule.id}:{tx.transaction_id}",
                    rule_id=rule.id,
                    rule_version=rule.version,
                    account_key=receiver,
                    triggered_at=now,
                    transaction_id=tx.transaction_id,
                    value=len(chain) + 1,
                    evidence=(tx.transaction_id, *reversed(chain)),
                )
        self.legs.add(now, sender, receiver, tx.transaction_id)
        return alert


class OnlineEvaluator:
    """Runs every rule on each transaction; fails on input out of event-time order."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        self._rules = [
            CycleRule(rule) if rule.metric == "short_cycle" else OnlineRule(rule) for rule in rules
        ]
        self._last: tuple[datetime, int] | None = None

    def observe(self, tx: Transaction) -> list[Alert]:
        position = (tx.transacted_at, tx.transaction_id)
        if self._last is not None and position <= self._last:
            raise ValueError(
                f"transaction {tx.transaction_id} at {tx.transacted_at} arrived out of "
                f"event-time order (last: {self._last[1]} at {self._last[0]})"
            )
        self._last = position
        return [alert for rule in self._rules if (alert := rule.observe(tx)) is not None]
