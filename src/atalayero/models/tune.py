"""Hyperparameter search with Optuna (ADR-0010).

Each family gets a seeded TPE study that maximises PR-AUC on validation, and its first trial is
the current configuration. The best trial is adopted only if it does not detect less laundering
without hubs, at the rules' volume, than that first trial; otherwise the family keeps its
parameters. LightGBM grows trees until validation PR-AUC stops improving, and a median pruner
stops trials that fall behind. The result becomes a new version of `config/models.yaml`, which
`make train` then fits.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import lightgbm
import mlflow
import numpy as np
import optuna
import yaml
from sklearn.metrics import average_precision_score

from atalayero.models.data import Dataset, load_split
from atalayero.models.evaluate import SplitEvaluator
from atalayero.models.families import (
    FAMILIES,
    FamilyName,
    ModelsConfig,
    Params,
    as_categories,
    fit,
    load_models_config,
    training_rows,
    transaction_scores,
)
from atalayero.models.registry import Registry
from atalayero.rules.schema import HistoryEntry, load_rules
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

MAX_TREES = 3000
PATIENCE = 100  # boosting rounds without a better validation PR-AUC
REPORT_EVERY = 50  # rounds between reports to the pruner


def _logistic_regression(trial: optuna.Trial) -> Params:
    return {
        "C": trial.suggest_float("C", 1e-4, 10.0, log=True),
        "class_weight": trial.suggest_categorical("class_weight", [None, "balanced"]),
        # Fitting slows down with more clean rows and, below 0.3, gains nothing (ADR-0010).
        "negative_rate": trial.suggest_float("negative_rate", 0.01, 0.3, log=True),
    }


def _lightgbm(trial: optuna.Trial) -> Params:
    return {
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 7, 255, log=True),
        "min_child_samples": trial.suggest_int("min_child_samples", 10, 2000, log=True),
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.3, 1.0),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 30.0, log=True),
        "negative_rate": trial.suggest_float("negative_rate", 0.01, 1.0, log=True),
    }


def _isolation_forest(trial: optuna.Trial) -> Params:
    return {
        "n_estimators": trial.suggest_int("n_estimators", 50, 500, log=True),
        "max_samples": trial.suggest_int("max_samples", 64, 8192, log=True),
        "max_features": trial.suggest_float("max_features", 0.3, 1.0),
    }


SPACES: dict[FamilyName, Callable[[optuna.Trial], Params]] = {
    "logistic_regression": _logistic_regression,
    "lightgbm": _lightgbm,
    "isolation_forest": _isolation_forest,
}


def pruning_callback(trial: optuna.Trial) -> Callable[[lightgbm.callback.CallbackEnv], None]:
    """Report validation PR-AUC to the pruner every REPORT_EVERY rounds; stop if it says so."""

    def callback(env: lightgbm.callback.CallbackEnv) -> None:
        rounds = env.iteration + 1
        if rounds % REPORT_EVERY:
            return
        [value] = [
            v for _, metric, v, _ in env.evaluation_result_list if metric == "average_precision"
        ]
        trial.report(value, rounds)
        if trial.should_prune():
            raise optuna.TrialPruned(f"pruned after {rounds} rounds")

    return callback


def _fit_lightgbm(
    params: Params, train: Dataset, validation: Dataset, seed: int, trial: optuna.Trial
) -> tuple[object, Params]:
    """Fit with early stopping on validation PR-AUC; the number of trees becomes a parameter."""
    model = FAMILIES["lightgbm"].build({**params, "n_estimators": MAX_TREES}, seed)
    model.set_params(model__metric="average_precision")
    keep = training_rows(train.labels, float(params["negative_rate"]), seed)
    model.fit(
        train.features[keep],
        train.labels[keep],
        model__eval_X=(as_categories(validation.features),),
        model__eval_y=(validation.labels,),
        model__callbacks=[
            lightgbm.early_stopping(PATIENCE, verbose=False),
            pruning_callback(trial),
        ],
    )
    return model, {**params, "n_estimators": int(model[-1].best_iteration_)}


@dataclass(frozen=True)
class Tuned:
    params: Params  # what the config gets: the best trial's, or `start` if vetoed
    pr_auc: float  # the PR-AUC of those parameters on validation
    primary: float  # their detection without hubs at the rules' volume
    best_primary: float  # the best PR-AUC trial's detection without hubs
    adopted: bool  # False when the veto kept `start`


def tune_family(
    name: FamilyName,
    start: Params,
    train: Dataset,
    validation: Dataset,
    trials: int,
    seed: int,
    primary: Callable[[np.ndarray], float],
) -> tuple[Tuned, optuna.Study]:
    """Search a family's parameters by validation PR-AUC. `start` is the first trial, so it must
    lie inside the search space. `primary` gives the primary metric of validation scores: the
    best trial replaces `start` only if it does not detect less (ADR-0010)."""
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=2 * REPORT_EVERY),
        study_name=name,
    )
    searched = {k: v for k, v in start.items() if k != "n_estimators" or name != "lightgbm"}
    study.enqueue_trial(searched)
    complete: dict[int, Params] = {}

    def objective(trial: optuna.Trial) -> float:
        params = SPACES[name](trial)
        if name == "lightgbm":
            model, params = _fit_lightgbm(params, train, validation, seed, trial)
        else:
            model = fit(name, params, train, seed)
        scores = transaction_scores(model, validation.features)
        value = float(average_precision_score(validation.labels, scores))
        detection = primary(scores)
        trial.set_user_attr("detection_without_hubs", detection)
        complete[trial.number] = params
        logger.info(
            "%s trial %d: PR-AUC %.4f, detection without hubs %.1f%%, with %s",
            name,
            trial.number,
            value,
            100 * detection,
            params,
        )
        return value

    study.optimize(objective, n_trials=trials)
    best, first = study.best_trial, study.trials[0]
    best_primary = best.user_attrs["detection_without_hubs"]
    start_primary = first.user_attrs["detection_without_hubs"]
    if best_primary < start_primary:
        logger.warning(
            "%s keeps its parameters: the best PR-AUC trial (%.4f) detects %.1f%% without hubs, "
            "the current ones %.1f%%",
            name,
            best.value,
            100 * best_primary,
            100 * start_primary,
        )
        return Tuned(start, first.value, start_primary, best_primary, False), study
    return Tuned(complete[best.number], best.value, best_primary, best_primary, True), study


def write_tuned_config(
    path: Path,
    current: ModelsConfig,
    tuned: dict[FamilyName, Tuned],
    trials: dict[str, int],
) -> ModelsConfig:
    """A new minor version of the config with the adopted parameters and a history entry."""
    major, minor = current.version.split(".")
    version = f"{major}.{int(minor) + 1}"
    outcomes = "; ".join(
        f"{name} PR-AUC {t.pr_auc:.4f}, detection without hubs {100 * t.primary:.1f}%"
        if t.adopted
        else f"{name} kept: best PR-AUC trial detects {100 * t.best_primary:.1f}% "
        f"without hubs against {100 * t.primary:.1f}%"
        for name, t in tuned.items()
    )
    entry = HistoryEntry(
        version=version,
        date=date.today(),
        reason=f"make tune: Optuna TPE, {sum(trials.values())} trials on validation. "
        f"{outcomes} (ADR-0010)",
    )
    config = ModelsConfig(
        version=version,
        families={
            name: {"params": tuned[name].params if name in tuned else family.params}
            for name, family in current.families.items()
        },
        history=(*current.history, entry),
    )
    header = (
        "# Hyperparameters of the model families (ADR-0009). `make tune` searches them with\n"
        "# Optuna and writes this file (ADR-0010); `make train` fits them. A change bumps\n"
        "# `version` and adds a `history` entry with the reason, as for the rules.\n"
        "# `negative_rate`: share of clean training rows kept; every laundering row is kept.\n"
    )
    path.write_text(header + yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))
    return load_models_config(path)


def log_study(name: FamilyName, result: Tuned, study: optuna.Study) -> int:
    """Log a family's study to the active MLflow run; returns the number of pruned trials."""
    pruned = sum(t.state == optuna.trial.TrialState.PRUNED for t in study.trials)
    mlflow.log_params(
        {
            "family": name,
            "trials": len(study.trials),
            "adopted": result.adopted,
            **{f"chosen/{k}": v for k, v in result.params.items()},
        }
    )
    mlflow.log_metrics(
        {
            "validation/pr_auc": result.pr_auc,
            "validation/detection_without_hubs": result.primary,
            "trials_pruned": pruned,
        }
    )
    for trial in study.trials:
        # Pruned trials carry their last reported PR-AUC as a value, but no detection.
        if trial.state == optuna.trial.TrialState.COMPLETE:
            mlflow.log_metric("trial/pr_auc", trial.value, step=trial.number)
            mlflow.log_metric(
                "trial/detection_without_hubs",
                trial.user_attrs["detection_without_hubs"],
                step=trial.number,
            )
    return pruned


def tune(settings: Settings) -> ModelsConfig:
    """Tune every family on validation, log the studies to MLflow and write the new config."""
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    current = load_models_config(settings.models.config_path)
    train = load_split(settings, "train")
    validation = load_split(settings, "validation")
    evaluator = SplitEvaluator(settings, "validation")
    evaluator.evaluate_rules(settings.rule_alerts_dir, load_rules(settings.rules_dir))
    budget = evaluator.rule_budget()

    def primary(scores: np.ndarray) -> float:
        evaluator.set_scores(validation.transaction_ids, scores)
        return evaluator.evaluate_scores("trial", budget).laundering_without_hubs.rate

    registry = Registry(settings)
    tuned: dict[FamilyName, Tuned] = {}
    for family, trials in settings.models.tuning_trials.items():
        if family not in FAMILIES:
            raise ValueError(f"unknown family {family!r} in models.tuning_trials")
        name = cast(FamilyName, family)
        result, study = tune_family(
            name,
            current.families[name].params,
            train,
            validation,
            trials,
            settings.models.seed,
            primary,
        )
        tuned[name] = result
        with mlflow.start_run(experiment_id=registry.experiment_id, run_name=f"tune-{name}"):
            pruned = log_study(name, result, study)
        logger.info(
            "%s: %s, validation PR-AUC %.4f (%d trials, %d pruned)",
            name,
            "adopted" if result.adopted else "kept",
            result.pr_auc,
            trials,
            pruned,
        )
    config = write_tuned_config(
        settings.models.config_path, current, tuned, settings.models.tuning_trials
    )
    logger.info(
        "Wrote %s v%s: run `make train` to fit it", settings.models.config_path, config.version
    )
    return config
