from collections.abc import Callable
from pathlib import Path

import numpy as np
import pydantic
import pytest
import yaml
from sklearn.metrics import average_precision_score

from atalayero.models.data import Dataset
from atalayero.models.families import (
    FAMILIES,
    ModelsConfig,
    fit,
    load_models_config,
    training_rows,
    transaction_scores,
)

REPO_ROOT = Path(__file__).parents[2]
CONFIG = REPO_ROOT / "config" / "models.yaml"

Synthetic = Callable[..., Dataset]


def test_repository_config_is_valid() -> None:
    config = load_models_config(CONFIG)

    assert set(config.families) == set(FAMILIES)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"version": "9.9"}, "add an entry with the reason"),
        ({"families": {"lightgbm": {"params": {"num_leaves": 31}}}}, "lightgbm takes exactly"),
        ({"families": {"random_forest": {"params": {}}}}, "Input should be"),
    ],
)
def test_rejects_invalid_configs(change: dict, error: str) -> None:
    raw = yaml.safe_load(CONFIG.read_text())

    with pytest.raises(pydantic.ValidationError, match=error):
        ModelsConfig.model_validate(raw | change)


def test_training_rows_keep_every_laundering_row() -> None:
    labels = np.zeros(10_000, dtype=bool)
    labels[:50] = True

    keep = training_rows(labels, 0.1, seed=0)

    assert keep[:50].all()
    assert 900 < keep[50:].sum() < 1100
    assert (keep == training_rows(labels, 0.1, seed=0)).all()


@pytest.mark.parametrize("name", list(FAMILIES))
def test_families_score_every_transaction(
    name: str, synthetic: Synthetic, small_params: dict[str, dict]
) -> None:
    train, validation = synthetic(seed=0), synthetic(seed=1)

    model = fit(name, small_params[name], train, seed=0)
    scores = transaction_scores(model, validation.features)

    assert scores.shape == (600,)
    assert np.isfinite(scores).all()
    if FAMILIES[name].supervised:  # the planted signal is easy
        assert average_precision_score(validation.labels, scores) > 0.5


def test_unsupervised_families_never_see_labels(
    synthetic: Synthetic, small_params: dict[str, dict]
) -> None:
    data = synthetic()
    unlabelled = Dataset(data.transaction_ids, data.features, np.array([], dtype=bool))

    fit("isolation_forest", small_params["isolation_forest"], unlabelled, seed=0)  # no error

    with pytest.raises(ValueError):
        fit("lightgbm", small_params["lightgbm"], unlabelled, seed=0)
