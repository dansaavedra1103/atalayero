import json
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml
from mlflow import MlflowClient

from atalayero.features.graph import build_graph_features
from atalayero.features.tabular import build_tabular_features
from atalayero.models.data import load_split
from atalayero.models.families import FAMILIES, transaction_scores
from atalayero.models.registry import Registry
from atalayero.models.train import train_and_evaluate
from atalayero.rules.schema import load_rules
from atalayero.schemas import Alert, Transaction
from atalayero.settings import Settings
from atalayero.streaming.consumer import AlertSink

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]

REPO_ROOT = Path(__file__).parents[2]
DAY = 24 * 60  # minutes; T0 is 2022-09-05 09:00


@pytest.fixture
def settings(
    make_tx: MakeTx,
    fct_transactions_db: WriteDb,
    small_params: dict[str, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """Four days of random transactions where large amounts are laundering: 5 Sep is warm-up,
    6-7 Sep train, 8 Sep validation. Features, rule alerts and a small model config are ready."""
    monkeypatch.chdir(REPO_ROOT)
    rng = random.Random(0)
    accounts = [f"00{i % 3}:{i}" for i in range(40)]
    transactions = [
        make_tx(
            i,
            rng.randrange(0, 4 * DAY - 540),
            sender=rng.choice(accounts),
            receiver=rng.choice(accounts),
            usd=rng.choice([500.0, 1500.0, 9500.0]),
        )
        for i in range(800)
    ]
    db = fct_transactions_db(transactions)
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE marts.fct_laundering_labels AS SELECT transaction_id, "
            "amount_paid_usd > 9000 AND transaction_id % 3 = 0 AS is_laundering, "
            "'untyped' AS label_group, NULL::INTEGER AS attempt_id, NULL AS typology "
            "FROM marts.fct_transactions"
        )
    config = tmp_path / "models.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "families": {name: {"params": small_params[name]} for name in FAMILIES},
                "history": [{"version": "1.0", "date": "2026-10-05", "reason": "Tests"}],
            }
        )
    )
    base = Settings()
    settings = base.model_copy(
        update={
            "duckdb_path": db,
            "features_dir": tmp_path / "features",
            "rule_alerts_dir": tmp_path / "alerts",
            "graph_workers": 1,
            "splits": base.splits.model_copy(
                update={
                    "train_end": datetime(2022, 9, 8),
                    "validation_end": datetime(2022, 9, 9),
                    "test_end": datetime(2022, 9, 10),
                }
            ),
            "evaluation": base.evaluation.model_copy(
                update={"hub_accounts": 1, "reports_dir": tmp_path / "reports"}
            ),
            "models": base.models.model_copy(
                update={
                    "config_path": config,
                    "train_start": datetime(2022, 9, 6),
                    "mlflow_tracking_uri": f"sqlite:///{tmp_path}/mlflow/mlflow.db",
                    "mlflow_artifacts_dir": tmp_path / "mlflow" / "artifacts",
                }
            ),
        }
    )
    build_tabular_features(settings)
    build_graph_features(settings)
    sink = AlertSink(settings.rule_alerts_dir)
    sink.extend(
        Alert(
            alert_id=f"R01:{i}",
            rule_id="R01",
            rule_version=load_rules(settings.rules_dir)[0].version,
            account_key=accounts[i],
            triggered_at=datetime(2022, 9, 8, 12) + timedelta(minutes=i),
            transaction_id=i,
            value=10,
            evidence=(i,),
        )
        for i in range(3)
    )
    sink.flush()
    return settings


def test_train_compares_every_family_with_the_rules(settings: Settings) -> None:
    path = train_and_evaluate(settings)

    report = json.loads(path.read_text())
    detectors = [r["detector"] for r in report["results"]]
    assert detectors[0].startswith("rules R01")
    for name in FAMILIES:
        assert name in detectors
        assert [f"{name} @ {n}/day" in detectors for n in settings.evaluation.budgets]
    assert len(detectors) == 1 + len(FAMILIES) * (1 + len(settings.evaluation.budgets))
    assert set(report["pr_auc"]) == set(FAMILIES)

    registry = Registry(settings)
    runs = MlflowClient(settings.models.mlflow_tracking_uri).search_runs([registry.experiment_id])
    assert sorted(r.info.run_name for r in runs) == sorted(["rules-only", *FAMILIES])
    assert registry.champion_value() is not None
    validation = load_split(settings, "validation")
    scores = transaction_scores(registry.load_champion(), validation.features)
    assert scores.shape == validation.labels.shape


def test_train_split_starts_after_the_warm_up(settings: Settings) -> None:
    train = load_split(settings, "train")
    validation = load_split(settings, "validation")

    assert train.features.shape[1] == validation.features.shape[1] == 43
    assert train.features.dtypes.astype(str).isin(["float64", "str", "object"]).all()
    with duckdb.connect(str(settings.duckdb_path), read_only=True) as con:
        (first,) = con.execute(
            "SELECT min(transacted_at) FROM marts.fct_transactions WHERE transaction_id IN "
            f"({', '.join(map(str, train.transaction_ids))})"
        ).fetchone()
    assert first >= datetime(2022, 9, 6)
