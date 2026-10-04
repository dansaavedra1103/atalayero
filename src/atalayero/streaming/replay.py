"""A replay: producer and consumer run side by side, talking only through Redpanda."""

import threading
from concurrent.futures import ThreadPoolExecutor

from atalayero.settings import Settings
from atalayero.streaming.consumer import consume
from atalayero.streaming.producer import iter_transactions, prepare_topic, produce


def replay(settings: Settings, limit: int | None = None) -> tuple[int, int]:
    """Replay the transactions from the start; return (transactions, alerts)."""
    stream = settings.streaming
    start = prepare_topic(stream.bootstrap_servers, stream.topic)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        transactions = iter_transactions(settings.duckdb_path, limit)
        produced = pool.submit(produce, transactions, stream, stop)
        try:
            alerts = consume(
                settings, start, lambda: produced.result() if produced.done() else None
            )
        except BaseException:
            stop.set()  # do not make a failed replay wait for the producer to finish
            raise
    return produced.result(), alerts
