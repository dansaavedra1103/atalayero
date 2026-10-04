"""Needs Redpanda (`make up`); run with `make test-integration`."""

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
from confluent_kafka import KafkaException
from confluent_kafka.admin import AdminClient

from atalayero.rules.online import OnlineEvaluator
from atalayero.rules.schema import load_rules
from atalayero.schemas import Alert, Transaction
from atalayero.settings import Settings
from atalayero.streaming.producer import iter_transactions
from atalayero.streaming.replay import replay

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).parents[2]
MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]


def test_replay_through_redpanda_matches_offline_evaluation(
    make_tx: MakeTx, fct_transactions_db: WriteDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    base = Settings()
    try:
        AdminClient({"bootstrap.servers": base.streaming.bootstrap_servers}).list_topics(timeout=5)
    except KafkaException:
        pytest.skip("Redpanda is not running: make up")

    transactions = [
        # R02: 6,000 USD to 12 counterparties within two hours
        *(make_tx(i, i * 10, receiver=f"002:C{i}") for i in range(1, 13)),
        # R04: 15 small payments in 30 minutes
        *(
            make_tx(100 + i, 200 + 2 * i, sender="003:X", receiver="004:Y", usd=50)
            for i in range(15)
        ),
        # quiet accounts
        *(make_tx(200 + i, 7 * i, sender=f"005:N{i}", receiver="006:M") for i in range(20)),
    ]
    db = fct_transactions_db(transactions)
    topic = f"test-replay-{uuid4().hex}"
    settings = Settings(
        duckdb_path=db,
        alerts_dir=tmp_path / "alerts",
        streaming=base.streaming.model_copy(update={"topic": topic}),
    )

    produced, written = replay(settings)

    evaluator = OnlineEvaluator(load_rules(settings.rules_dir))
    expected = [alert for tx in iter_transactions(db) for alert in evaluator.observe(tx)]
    rel = duckdb.sql(f"SELECT * FROM read_parquet('{settings.alerts_dir}/part-*.parquet')")
    got = [Alert(**dict(zip(rel.columns, row, strict=True))) for row in rel.fetchall()]
    assert produced == len(transactions)
    assert {a.rule_id for a in expected} == {"R02", "R04"}
    assert written == len(expected)
    assert sorted(got, key=lambda a: a.alert_id) == sorted(expected, key=lambda a: a.alert_id)
