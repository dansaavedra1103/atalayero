import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import mlflow
import optuna
import pytest
from mlflow import MlflowClient

from atalayero.models.data import Dataset
from atalayero.models.families import FAMILIES, load_models_config
from atalayero.models.registry import Registry
from atalayero.models.tune import (
    MAX_TREES,
    REPORT_EVERY,
    Tuned,
    log_study,
    pruning_callback,
    tune,
    tune_family,
    write_tuned_config,
)
from atalayero.settings import Settings

REPO_ROOT = Path(__file__).parents[2]
Synthetic = Callable[..., Dataset]


@pytest.fixture(autouse=True)
def _quiet_optuna() -> None:
    optuna.logging.set_verbosity(optuna.logging.WARNING)


@pytest.mark.parametrize("name", list(FAMILIES))
def test_tuning_starts_from_the_current_parameters(
    name: str, synthetic: Synthetic, small_params: dict[str, dict]
) -> None:
    train, validation = synthetic(seed=0), synthetic(seed=1)

    tuned, study = tune_family(
        name, small_params[name], train, validation, 3, seed=0, primary=lambda _: 0.5
    )

    assert tuned.adopted  # ties on the primary metric adopt the best PR-AUC
    assert set(tuned.params) == FAMILIES[name].params
    assert 0 < tuned.pr_auc <= 1
    assert tuned.pr_auc == max(t.value for t in study.trials if t.value is not None)
    first = {k: v for k, v in small_params[name].items() if k != "n_estimators"}
    assert {k: study.trials[0].params[k] for k in first} == first
    if name == "lightgbm":
        assert 1 <= tuned.params["n_estimators"] <= MAX_TREES


def test_a_trial_that_detects_less_cannot_replace_the_current_parameters(
    synthetic: Synthetic, small_params: dict[str, dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    train, validation = synthetic(seed=0), synthetic(seed=1)
    pr_aucs = iter([0.10, 0.50, 0.30, 0.20])  # trial 1 has the best PR-AUC...
    detections = iter([0.30, 0.10, 0.20, 0.25])  # ...but trial 0, the current one, detects most
    monkeypatch.setattr("atalayero.models.tune.average_precision_score", lambda *_: next(pr_aucs))
    start = small_params["logistic_regression"]

    tuned, study = tune_family(
        "logistic_regression", start, train, validation, 4, 0, lambda _: next(detections)
    )

    assert study.best_trial.number == 1
    assert not tuned.adopted
    assert tuned.params == start
    assert (tuned.pr_auc, tuned.primary, tuned.best_primary) == (0.10, 0.30, 0.10)


def test_the_pruner_stops_a_lagging_trial() -> None:
    reports: list[tuple[float, int]] = []
    trial = SimpleNamespace(
        report=lambda value, step: reports.append((value, step)), should_prune=lambda: True
    )
    callback = pruning_callback(trial)  # type: ignore[arg-type]

    def env(rounds: int) -> SimpleNamespace:
        return SimpleNamespace(
            iteration=rounds - 1,
            evaluation_result_list=[("valid_0", "average_precision", 0.3, True)],
        )

    callback(env(REPORT_EVERY - 1))  # not a reporting round
    with pytest.raises(optuna.TrialPruned):
        callback(env(REPORT_EVERY))
    assert reports == [(0.3, REPORT_EVERY)]


def test_tuned_parameters_become_a_new_config_version(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    shutil.copy(REPO_ROOT / "config" / "models.yaml", path)
    current = load_models_config(path)
    tuned_lr = {"C": 0.5, "class_weight": None, "negative_rate": 0.05}
    kept_if = current.families["isolation_forest"].params

    config = write_tuned_config(
        path,
        current,
        {
            "logistic_regression": Tuned(tuned_lr, 0.12, 0.08, 0.08, True),
            "isolation_forest": Tuned(kept_if, 0.004, 0.02, 0.01, False),
        },
        {"x": 4},
    )

    major, minor = current.version.split(".")
    assert config.version == f"{major}.{int(minor) + 1}"
    assert config.history[:-1] == current.history
    reason = config.history[-1].reason
    assert "4 trials on validation" in reason
    assert "logistic_regression PR-AUC 0.1200, detection without hubs 8.0%" in reason
    assert "isolation_forest kept: best PR-AUC trial detects 1.0% without hubs against 2.0%" in (
        reason
    )
    assert config.families["logistic_regression"].params == tuned_lr
    assert config.families["isolation_forest"].params == kept_if
    assert config.families["lightgbm"] == current.families["lightgbm"]
    assert path.read_text().startswith("# Hyperparameters of the model families")


def test_tune_writes_the_config_and_logs_to_mlflow(model_settings: Settings) -> None:
    trials = {"logistic_regression": 2, "lightgbm": 2, "isolation_forest": 2}
    settings = model_settings.model_copy(
        update={"models": model_settings.models.model_copy(update={"tuning_trials": trials})}
    )
    before = load_models_config(settings.models.config_path)

    after = tune(settings)

    assert after.version != before.version
    assert load_models_config(settings.models.config_path) == after
    registry = Registry(settings)
    runs = MlflowClient(settings.models.mlflow_tracking_uri).search_runs([registry.experiment_id])
    assert sorted(r.info.run_name for r in runs) == sorted(f"tune-{name}" for name in trials)


def test_study_logging_skips_what_pruned_trials_lack(model_settings: Settings) -> None:
    def objective(trial: optuna.Trial) -> float:
        trial.suggest_float("x", 0, 1)
        if trial.number == 1:
            trial.report(0.2, 50)
            raise optuna.TrialPruned
        trial.set_user_attr("detection_without_hubs", 0.1)
        return 0.3

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=3)
    assert study.trials[1].value is not None  # optuna keeps the last report as its value
    result = Tuned({"C": 1.0}, 0.3, 0.1, 0.1, True)
    registry = Registry(model_settings)

    with mlflow.start_run(experiment_id=registry.experiment_id):
        pruned = log_study("logistic_regression", result, study)

    assert pruned == 1
