from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest

from atalayero.rules.batch import evaluate_rules
from atalayero.rules.online import OnlineEvaluator
from atalayero.rules.schema import load_rules
from atalayero.schemas import Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]

REPO_ROOT = Path(__file__).parents[2]


def test_batch_alerts_match_the_online_engine_before_the_end_of_test(
    make_tx: MakeTx, fct_transactions_db: WriteDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    cycle = [
        make_tx(100, 0, sender="003:A", receiver="003:B"),
        make_tx(101, 5, sender="003:B", receiver="003:A"),  # R03
    ]
    fan_out = [make_tx(i, i * 10, receiver=f"002:C{i}") for i in range(1, 13)]  # R02 at the 10th
    db = fct_transactions_db([*cycle, *fan_out])
    settings = Settings(duckdb_path=db, rule_alerts_dir=tmp_path / "alerts")
    # The fan-out reaches R02's threshold after the end of the test split, which is never read.
    end = fan_out[6].transacted_at
    settings = settings.model_copy(
        update={"splits": settings.splits.model_copy(update={"test_end": end})}
    )

    transactions, written = evaluate_rules(settings)

    evaluator = OnlineEvaluator(load_rules(settings.rules_dir))
    early = [tx for tx in [*cycle, *fan_out] if tx.transacted_at < end]
    expected = sorted(a.alert_id for tx in early for a in evaluator.observe(tx))
    got = duckdb.sql(
        f"SELECT alert_id FROM read_parquet('{tmp_path}/alerts/part-*.parquet') ORDER BY 1"
    ).fetchall()
    assert transactions == len(early) == 8
    assert expected == ["R03:101"]
    assert (written, [a for (a,) in got]) == (1, expected)
