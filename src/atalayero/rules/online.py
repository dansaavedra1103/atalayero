"""Online evaluation of the rules over a transaction stream, in event-time order.

Each rule keeps, per sender account, the outgoing transactions inside its trailing window
(`transacted_at >= t - window`, both ends included). Only transactions already seen count, so the
evaluation cannot look ahead in time.
"""

from collections import Counter, deque
from collections.abc import Iterable
from datetime import datetime
from itertools import islice

from atalayero.rules.schema import Rule
from atalayero.schemas import Alert, Transaction

MAX_EVIDENCE = 100  # transaction IDs kept in an alert; `value` holds the full measure


class _Window:
    __slots__ = ("counterparties", "entries")

    def __init__(self) -> None:
        self.entries: deque[tuple[datetime, str, int]] = deque()  # (time, receiver, id)
        self.counterparties: Counter[str] = Counter()


class OnlineRule:
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
        rule, account = self.rule, tx.sender_account_key
        if self._next_sweep is None or tx.transacted_at >= self._next_sweep:
            self._sweep(tx.transacted_at)
            self._next_sweep = tx.transacted_at + rule.window
        if account == tx.receiver_account_key or tx.amount_paid_usd < rule.threshold.min_amount_usd:
            return None

        window = self._windows.setdefault(account, _Window())
        window.entries.append((tx.transacted_at, tx.receiver_account_key, tx.transaction_id))
        window.counterparties[tx.receiver_account_key] += 1
        start = tx.transacted_at - rule.window
        while window.entries[0][0] < start:
            _, receiver, _ = window.entries.popleft()
            window.counterparties[receiver] -= 1
            if not window.counterparties[receiver]:
                del window.counterparties[receiver]

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


class OnlineEvaluator:
    """Runs every rule on each transaction; fails on input out of event-time order."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        self._rules = [OnlineRule(rule) for rule in rules]
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
