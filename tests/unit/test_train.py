import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import duckdb
from mlflow import MlflowClient

from atalayero.models.data import FEATURES, load_split
from atalayero.models.families import FAMILIES, transaction_scores
from atalayero.models.registry import Registry
from atalayero.models.train import train_and_evaluate
from atalayero.schemas import Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]

REPO_ROOT = Path(__file__).parents[2]
DAY = 24 * 60  # minutes; T0 is 2022-09-05 09:00


def test_train_compares_every_family_with_the_rules(model_settings: Settings) -> None:
    settings = model_settings

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


def test_train_split_starts_after_the_warm_up(model_settings: Settings) -> None:
    settings = model_settings

    train = load_split(settings, "train")
    validation = load_split(settings, "validation")

    assert train.features.shape[1] == validation.features.shape[1] == len(FEATURES)
    assert train.features.dtypes.astype(str).isin(["float64", "str", "object"]).all()
    with duckdb.connect(str(settings.duckdb_path), read_only=True) as con:
        (first,) = con.execute(
            "SELECT min(transacted_at) FROM marts.fct_transactions WHERE transaction_id IN "
            f"({', '.join(map(str, train.transaction_ids))})"
        ).fetchone()
    assert first >= datetime(2022, 9, 6)
