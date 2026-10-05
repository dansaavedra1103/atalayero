from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from atalayero.models.data import Dataset
from atalayero.models.families import fit, transaction_scores
from atalayero.models.registry import Registry
from atalayero.settings import Settings

REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.chdir(REPO_ROOT)
    base = Settings()
    return base.model_copy(
        update={
            "models": base.models.model_copy(
                update={
                    "mlflow_tracking_uri": f"sqlite:///{tmp_path}/mlflow/mlflow.db",
                    "mlflow_artifacts_dir": tmp_path / "mlflow" / "artifacts",
                }
            )
        }
    )


def test_the_champion_changes_only_for_a_better_model(
    settings: Settings, synthetic: Callable[..., Dataset], small_params: dict[str, dict]
) -> None:
    data = synthetic()
    model = fit("logistic_regression", small_params["logistic_regression"], data, seed=0)
    registry = Registry(settings)

    def log(value: float) -> str:
        _, uri = registry.log_run(
            "test", {"family": "logistic_regression"}, {"m": value}, model=model
        )
        assert uri is not None
        return uri

    assert registry.champion_value() is None
    assert registry.promote(log(0.10), 0.10)
    assert not registry.promote(log(0.08), 0.08)
    assert not registry.promote(log(0.10), 0.10)  # a tie keeps the champion
    assert registry.promote(log(0.12), 0.12)
    assert registry.champion_value() == 0.12

    champion = Registry(settings).load_champion()
    expected = transaction_scores(model, data.features)
    assert np.allclose(transaction_scores(champion, data.features), expected)
