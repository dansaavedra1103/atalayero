"""Producer: replay `marts.fct_transactions` into Redpanda in event-time order."""

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path

import duckdb
from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from atalayero.schemas import Transaction
from atalayero.settings import StreamingSettings

logger = logging.getLogger(__name__)

FIELDS = tuple(Transaction.model_fields)


def iter_transactions(
    duckdb_path: Path, limit: int | None = None, until: datetime | None = None
) -> Iterator[Transaction]:
    """Yield transactions (no labels) by event time, before `until` if given; same-minute ties
    by transaction_id."""
    query = f"SELECT {', '.join(FIELDS)} FROM marts.fct_transactions"
    if until is not None:
        query += " WHERE transacted_at < $until"
    query += " ORDER BY transacted_at, transaction_id"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        con.execute("SET enable_progress_bar = false")
        cursor = con.execute(query, {"until": until} if until is not None else None)
        while rows := cursor.fetchmany(10_000):
            for row in rows:
                yield Transaction(**dict(zip(FIELDS, row, strict=True)))


class Pacer:
    """Sleeps so that event time runs at most `speedup` times faster than real time (0: never)."""

    def __init__(
        self,
        speedup: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._speedup, self._clock, self._sleep = speedup, clock, sleep
        self._origin: tuple[datetime, float] | None = None

    def wait(self, event_time: datetime) -> None:
        if not self._speedup:
            return
        if self._origin is None:
            self._origin = (event_time, self._clock())
            return
        first_event, start = self._origin
        due = start + (event_time - first_event).total_seconds() / self._speedup
        if (delay := due - self._clock()) > 0:
            self._sleep(delay)


def prepare_topic(bootstrap_servers: str, topic: str) -> int:
    """Create `topic` with one partition (total order) if missing; return its end offset, where
    a new replay starts."""
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    topics = admin.list_topics(timeout=10).topics
    if topic not in topics:
        admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])[
            topic
        ].result()
    elif len(topics[topic].partitions) != 1:
        raise ValueError(f"topic {topic!r} must have exactly one partition")

    consumer = Consumer({"bootstrap.servers": bootstrap_servers, "group.id": "atalayero-offsets"})
    try:
        for _ in range(20):  # a new topic takes a moment to be served
            try:
                _, end = consumer.get_watermark_offsets(TopicPartition(topic, 0), timeout=5)
                return end
            except KafkaException:
                time.sleep(0.5)
        raise TimeoutError(f"cannot read the offsets of topic {topic!r}")
    finally:
        consumer.close()


def produce(
    transactions: Iterable[Transaction],
    stream: StreamingSettings,
    stop: threading.Event | None = None,
) -> int:
    """Send every transaction as JSON, keyed by sender account, until `stop` is set; return how
    many were sent."""
    producer = Producer({"bootstrap.servers": stream.bootstrap_servers, "enable.idempotence": True})
    failures: list[object] = []
    pacer = Pacer(stream.speedup)
    sent = 0
    for tx in transactions:
        if stop is not None and stop.is_set():
            break
        pacer.wait(tx.transacted_at)
        payload = tx.model_dump_json().encode()
        while True:
            try:
                producer.produce(
                    stream.topic,
                    key=tx.sender_account_key.encode(),
                    value=payload,
                    on_delivery=lambda err, _msg: err and failures.append(err),
                )
                break
            except BufferError:  # local queue full: let deliveries drain
                producer.poll(0.5)
        producer.poll(0)
        sent += 1
        if failures:
            raise KafkaException(failures[0])
    if producer.flush(60) or failures:
        raise KafkaException(failures[0] if failures else "messages left undelivered")
    logger.info("Produced %d transactions to %s", sent, stream.topic)
    return sent
