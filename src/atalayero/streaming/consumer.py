"""Consumer: evaluate the rules on the transaction stream and write the alerts to Parquet."""

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from uuid import uuid4

import duckdb
from confluent_kafka import Consumer, KafkaException, TopicPartition

from atalayero.ingestion.source import sql_literal
from atalayero.rules.online import OnlineEvaluator
from atalayero.rules.schema import load_rules
from atalayero.schemas import Alert, Transaction
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

# The columns of an alerts file.
ALERT_COLUMNS = (
    "alert_id VARCHAR, rule_id VARCHAR, rule_version VARCHAR, account_key VARCHAR, "
    "triggered_at TIMESTAMP, transaction_id BIGINT, value DOUBLE, evidence BIGINT[]"
)


def write_alerts(alerts: Sequence[Alert], path: Path) -> None:
    """Write alerts to a Parquet file, one column per `Alert` field, with the same column types
    whatever the alerts (or none)."""
    columns = {name: [getattr(a, name) for a in alerts] for name in Alert.model_fields}
    columns["evidence"] = [list(e) for e in columns["evidence"]]
    with duckdb.connect() as con:
        con.execute(f"CREATE TABLE alerts ({ALERT_COLUMNS})")
        if alerts:
            select = ", ".join(f"unnest(${name})" for name in columns)
            con.execute(f"INSERT INTO alerts SELECT {select}", columns)
        con.execute(f"COPY alerts TO {sql_literal(path)} (FORMAT parquet)")


class AlertSink:
    """Writes alerts as `part-NNNNN.parquet` files, replacing the parts of the previous run."""

    def __init__(self, alerts_dir: Path, batch_size: int = 1000) -> None:
        alerts_dir.mkdir(parents=True, exist_ok=True)
        for old in alerts_dir.glob("part-*.parquet"):
            old.unlink()
        self._dir, self._batch_size = alerts_dir, batch_size
        self._buffer: list[Alert] = []
        self._parts = 0
        self.written = 0

    def extend(self, alerts: Iterable[Alert]) -> None:
        self._buffer.extend(alerts)
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        write_alerts(self._buffer, self._dir / f"part-{self._parts:05d}.parquet")
        self._parts += 1
        self.written += len(self._buffer)
        self._buffer.clear()


def consume(settings: Settings, start_offset: int, expected: Callable[[], int | None]) -> int:
    """Read the topic from `start_offset` until `expected()` messages have been read (it returns
    None while the producer is still running); return the number of alerts written."""
    stream = settings.streaming
    consumer = Consumer(
        {
            "bootstrap.servers": stream.bootstrap_servers,
            "group.id": f"atalayero-replay-{uuid4()}",
            "enable.auto.commit": False,
        }
    )
    consumer.assign([TopicPartition(stream.topic, 0, start_offset)])
    evaluator = OnlineEvaluator(load_rules(settings.rules_dir))
    sink = AlertSink(settings.alerts_dir)
    consumed, last_message = 0, time.monotonic()
    try:
        while (total := expected()) is None or consumed < total:
            messages = consumer.consume(num_messages=1000, timeout=1.0)
            if not messages:
                # While the producer runs, gaps are legitimate (pacing) and its errors surface
                # through expected(); once it is done, missing messages mean a broken stream.
                idle = time.monotonic() - last_message
                if total is not None and idle > stream.idle_timeout_seconds:
                    raise TimeoutError(
                        f"{total - consumed} messages still missing after {idle:.0f} s without any"
                    )
                continue
            last_message = time.monotonic()
            for message in messages:
                if message.error():
                    raise KafkaException(message.error())
                sink.extend(evaluator.observe(Transaction.model_validate_json(message.value())))
            consumed += len(messages)
        sink.flush()
    finally:
        consumer.close()
    logger.info(
        "Consumed %d transactions, wrote %d alerts to %s",
        consumed,
        sink.written,
        settings.alerts_dir,
    )
    return sink.written
