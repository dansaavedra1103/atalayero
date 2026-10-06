"""The single test run (ADR-0014): refit every family on train and validation with the
hyperparameters chosen on validation, then compare it with the rules on test (ADR-0005,
ADR-0007).

Nothing is selected here. The family that leads was chosen on validation by `make train`. The
refit models are logged in MLflow but never registered: they have no validation metric left to
compete on.
"""

import logging
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from atalayero.models.data import FEATURES, load_period, load_split
from atalayero.models.evaluate import SplitEvaluator
from atalayero.models.families import fit, load_models_config, transaction_scores
from atalayero.models.registry import Registry
from atalayero.models.train import (
    Evaluated,
    evaluate_detector,
    log_detector,
    split_metrics,
    warn_if_it_loses,
    write_report,
)
from atalayero.rules.schema import load_rules
from atalayero.settings import Settings

logger = logging.getLogger(__name__)


def run_holdout(settings: Settings) -> Path:
    """Fit on `[models.train_start, splits.validation_end)` and evaluate on test. Writes
    `<reports_dir>/holdout_test.json`, and the test scores of every family to
    `<reports_dir>/holdout_scores.parquet`; returns the report's path."""
    logger.warning("Reading the test split: once per reported result (ADR-0005)")
    config = load_models_config(settings.models.config_path)
    rules = load_rules(settings.rules_dir)
    fit_start, fit_end = settings.models.train_start, settings.splits.validation_end
    data = load_period(settings, fit_start, fit_end)
    test = load_split(settings, "test")
    logger.info(
        "Fit on train and validation: %d rows (%d laundering); test: %d rows",
        len(data.labels),
        data.labels.sum(),
        len(test.labels),
    )
    evaluator = SplitEvaluator(settings, "test")
    baseline = evaluator.evaluate_rules(settings.rule_alerts_dir, rules)
    budget = evaluator.rule_budget()
    registry = Registry(settings)
    common = {
        "split": "test",
        "models_config": config.version,
        "rules": ", ".join(f"{r.id} v{r.version}" for r in rules),
        "fit_start": fit_start.date().isoformat(),
        "fit_end": fit_end.date().isoformat(),
        "test_end": settings.splits.test_end.date().isoformat(),
        "features": len(FEATURES),
        "seed": settings.models.seed,
    }
    registry.log_run("rules-only on test", common, split_metrics(baseline))

    results: dict[str, Evaluated] = {}
    scores: dict[str, np.ndarray] = {"transaction_id": test.transaction_ids}
    for name, family in config.families.items():
        start = time.monotonic()
        model = fit(name, family.params, data, settings.models.seed)
        seconds = time.monotonic() - start
        scores[name] = transaction_scores(model, test.features)
        result = evaluate_detector(
            evaluator,
            name,
            test.transaction_ids,
            scores[name],
            budget,
            settings.evaluation.budgets,
        )
        results[name] = result
        logger.info(
            "%s: fit in %.0f s, PR-AUC %.4f, detection without hubs %.1f%% at %.1f alerts/day",
            name,
            seconds,
            result.pr_auc,
            100 * result.matched.laundering_without_hubs.rate,
            result.matched.alerts_per_day,
        )
        log_detector(
            registry,
            f"{name} on test",
            {**common, "family": name, **family.params},
            result,
            model,
            seconds,
        )
        warn_if_it_loses(result, baseline)

    path = write_report(settings, "holdout_test", "test", baseline, results)
    scores_path = settings.evaluation.reports_dir / "holdout_scores.parquet"
    with duckdb.connect() as con:
        con.from_df(pd.DataFrame(scores)).write_parquet(str(scores_path))
    logger.info("Test scores of every family written to %s", scores_path)
    return path
