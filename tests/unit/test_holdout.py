import json
from datetime import datetime

import duckdb
import numpy as np
import pytest
from mlflow import MlflowClient

from atalayero.models import holdout
from atalayero.models.data import Dataset, load_split
from atalayero.models.families import FAMILIES
from atalayero.models.holdout import run_holdout
from atalayero.models.registry import Registry
from atalayero.settings import Settings


@pytest.fixture
def holdout_settings(model_settings: Settings) -> Settings:
    """The model fixture with every split a day earlier, so that test holds 8 Sep: train is
    6 Sep, validation 7 Sep, and the rule alerts fall on test."""
    return model_settings.model_copy(
        update={
            "splits": model_settings.splits.model_copy(
                update={
                    "train_end": datetime(2022, 9, 7),
                    "validation_end": datetime(2022, 9, 8),
                    "test_end": datetime(2022, 9, 9),
                }
            )
        }
    )


def test_holdout_fits_on_train_and_validation(
    holdout_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fitted: list[Dataset] = []
    real_fit = holdout.fit

    def spy(name: str, params: dict, data: Dataset, seed: int) -> object:
        fitted.append(data)
        return real_fit(name, params, data, seed)

    monkeypatch.setattr(holdout, "fit", spy)

    run_holdout(holdout_settings)

    assert len(fitted) == len(FAMILIES)
    for data in fitted:
        days = set(data.days.astype(str))
        assert days == {"2022-09-06", "2022-09-07"}  # train after the warm-up, and validation


def test_holdout_reports_every_family_against_the_rules_on_test(
    holdout_settings: Settings,
) -> None:
    settings = holdout_settings

    path = run_holdout(settings)

    report = json.loads(path.read_text())
    assert path.name == "holdout_test.json"
    assert report["split"] == "test"
    detectors = [r["detector"] for r in report["results"]]
    assert detectors[0].startswith("rules R01")
    assert report["results"][0]["alerts"] == 3  # the fixture's rule alerts, on 8 Sep
    assert len(detectors) == 1 + len(FAMILIES) * (1 + len(settings.evaluation.budgets))
    assert set(report["pr_auc"]) == set(report["curves"]) == set(FAMILIES)

    test = load_split(settings, "test")
    scores = duckdb.read_parquet(str(settings.evaluation.reports_dir / "holdout_scores.parquet"))
    frame = scores.order("transaction_id").df()
    assert frame.columns[0] == "transaction_id"
    assert set(frame.columns[1:]) == set(FAMILIES)
    assert np.array_equal(frame["transaction_id"], test.transaction_ids)

    registry = Registry(settings)
    client = MlflowClient(settings.models.mlflow_tracking_uri)
    runs = client.search_runs([registry.experiment_id])
    assert sorted(r.info.run_name for r in runs) == sorted(
        ["rules-only on test", *(f"{name} on test" for name in FAMILIES)]
    )
    assert {r.data.params["fit_end"] for r in runs} == {"2022-09-08"}
    assert client.search_model_versions() == []  # refit models are never registered
