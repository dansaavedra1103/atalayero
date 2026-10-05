"""Batch evaluation: the online rule engine over the stored transactions, without Redpanda.

Same evaluator and same alert format as the replay (`make stream`), so batch and stream alerts
agree one by one. Only transactions before the end of the test split are read (ADR-0005).
"""

import logging

from atalayero.rules.online import OnlineEvaluator
from atalayero.rules.schema import load_rules
from atalayero.settings import Settings
from atalayero.streaming.consumer import AlertSink
from atalayero.streaming.producer import iter_transactions

logger = logging.getLogger(__name__)


def evaluate_rules(settings: Settings) -> tuple[int, int]:
    """Evaluate every rule and write the alerts to `rule_alerts_dir`; return (transactions,
    alerts)."""
    rules = load_rules(settings.rules_dir)
    evaluator = OnlineEvaluator(rules)
    sink = AlertSink(settings.rule_alerts_dir)
    transactions = 0
    for tx in iter_transactions(settings.duckdb_path, until=settings.splits.test_end):
        sink.extend(evaluator.observe(tx))
        transactions += 1
    sink.flush()
    logger.info(
        "Evaluated %s on %d transactions: %d alerts in %s",
        ", ".join(f"{r.id} v{r.version}" for r in rules),
        transactions,
        sink.written,
        settings.rule_alerts_dir,
    )
    return transactions, sink.written
