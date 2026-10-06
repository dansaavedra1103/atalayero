"""Fit each model family with its configured hyperparameters, evaluate it on validation against
the rules (ADR-0007) and register the best one in MLflow (ADR-0009)."""

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
from sklearn.pipeline import Pipeline

from atalayero.models.data import FEATURES, load_split
from atalayero.models.evaluate import AlertMetrics, CurvePoint, EvaluationReport, SplitEvaluator
from atalayero.models.families import fit, load_models_config, transaction_scores
from atalayero.models.registry import Registry
from atalayero.rules.schema import load_rules
from atalayero.settings import Settings, Split

logger = logging.getLogger(__name__)

CURVE_BUDGETS = (10, 25, 50, 75, 100, 150, 200, 300, 400, 600, 800, 1000)  # alerts per day


@dataclass(frozen=True)
class Evaluated:
    """A scored detector on one split."""

    matched: AlertMetrics  # at the rules' volume, day by day
    fixed: list[AlertMetrics]  # at each budget of `evaluation.budgets`
    pr_auc: float
    curve: list[CurvePoint]


def evaluate_detector(
    evaluator: SplitEvaluator,
    name: str,
    transaction_ids: np.ndarray,
    scores: np.ndarray,
    budget: Mapping[date, int],
    budgets: Iterable[int],
) -> Evaluated:
    """Evaluate a detector's transaction scores at the rules' volume, at fixed budgets, as a curve
    and by PR-AUC."""
    evaluator.set_scores(transaction_ids, scores)
    return Evaluated(
        matched=evaluator.evaluate_scores(name, budget),
        fixed=[evaluator.evaluate_scores(f"{name} @ {n}/day", n) for n in budgets],
        pr_auc=evaluator.pr_auc(),
        curve=evaluator.curve(CURVE_BUDGETS),
    )


def split_metrics(m: AlertMetrics) -> dict[str, float]:
    return {
        f"{m.split}/detection_without_hubs": m.laundering_without_hubs.rate,
        f"{m.split}/detection": m.laundering.rate,
        f"{m.split}/false_positive_share": m.false_positive_share,
        f"{m.split}/hub_alert_share": m.hub_alert_share,
        f"{m.split}/alerts_per_day": m.alerts_per_day,
        f"{m.split}/attempts": m.attempts.rate,
        f"{m.split}/patterned": m.patterned.rate,
        f"{m.split}/untyped": m.untyped.rate,
    }


def log_detector(
    registry: Registry,
    run_name: str,
    params: dict[str, object],
    result: Evaluated,
    model: Pipeline,
    fit_seconds: float,
) -> str:
    """One MLflow run for a fitted family and its evaluation; returns the model URI."""
    split = result.matched.split
    _, model_uri = registry.log_run(
        run_name,
        params,
        {
            **split_metrics(result.matched),
            f"{split}/pr_auc": result.pr_auc,
            "fit_seconds": fit_seconds,
        },
        curves={
            f"{split}/curve/detection_without_hubs": [
                (p.budget, p.detection_without_hubs) for p in result.curve
            ],
            f"{split}/curve/false_positive_share": [
                (p.budget, p.false_positive_share) for p in result.curve
            ],
        },
        artifacts={
            f"{split}.json": EvaluationReport(
                split=split,
                generated_at=datetime.now(UTC),
                results=[result.matched, *result.fixed],
                curves={result.matched.detector: result.curve},
                pr_auc={result.matched.detector: result.pr_auc},
            ).model_dump_json(indent=2)
        },
        model=model,
    )
    assert model_uri is not None
    return model_uri


def warn_if_it_loses(result: Evaluated, baseline: AlertMetrics) -> None:
    """Rule 7: a family that does not beat the rules is reported, not hidden."""
    if result.matched.laundering_without_hubs.rate <= baseline.laundering_without_hubs.rate:
        logger.warning(
            "%s does not beat the rules on %s: %.1f%% against %.1f%% detection without hubs",
            result.matched.detector,
            result.matched.split,
            100 * result.matched.laundering_without_hubs.rate,
            100 * baseline.laundering_without_hubs.rate,
        )


def write_report(
    settings: Settings,
    name: str,
    split: Split,
    baseline: AlertMetrics,
    results: Mapping[str, Evaluated],
) -> Path:
    """Write `<reports_dir>/<name>.json`: the rules, then each family; return its path."""
    report = EvaluationReport(
        split=split,
        generated_at=datetime.now(UTC),
        results=[baseline, *(m for r in results.values() for m in (r.matched, *r.fixed))],
        curves={family: r.curve for family, r in results.items()},
        pr_auc={family: r.pr_auc for family, r in results.items()},
    )
    settings.evaluation.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.evaluation.reports_dir / f"{name}.json"
    path.write_text(report.model_dump_json(indent=2))
    logger.info("Models against the rules on %s, written to %s:\n%s", split, path, report.table())
    return path


def train_and_evaluate(settings: Settings) -> Path:
    """Train every family, compare it with the rules on validation, register the best by
    detection without hubs at the rules' volume (ties: PR-AUC). Writes
    `<reports_dir>/models_validation.json` and returns its path."""
    config = load_models_config(settings.models.config_path)
    rules = load_rules(settings.rules_dir)
    train = load_split(settings, "train")
    validation = load_split(settings, "validation")
    logger.info(
        "Train: %d rows (%d laundering); validation: %d rows",
        len(train.labels),
        train.labels.sum(),
        len(validation.labels),
    )
    evaluator = SplitEvaluator(settings, "validation")
    baseline = evaluator.evaluate_rules(settings.rule_alerts_dir, rules)
    budget = evaluator.rule_budget()
    registry = Registry(settings)
    common = {
        "models_config": config.version,
        "rules": ", ".join(f"{r.id} v{r.version}" for r in rules),
        "train_start": settings.models.train_start.date().isoformat(),
        "train_end": settings.splits.train_end.date().isoformat(),
        "validation_end": settings.splits.validation_end.date().isoformat(),
        "features": len(FEATURES),
        "seed": settings.models.seed,
    }
    registry.log_run("rules-only", common, split_metrics(baseline))

    results: dict[str, Evaluated] = {}
    candidates: list[tuple[float, float, str, str]] = []
    for name, family in config.families.items():
        start = time.monotonic()
        model = fit(name, family.params, train, settings.models.seed)
        seconds = time.monotonic() - start
        result = evaluate_detector(
            evaluator,
            name,
            validation.transaction_ids,
            transaction_scores(model, validation.features),
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
        model_uri = log_detector(
            registry, name, {**common, "family": name, **family.params}, result, model, seconds
        )
        candidates.append(
            (result.matched.laundering_without_hubs.rate, result.pr_auc, name, model_uri)
        )
        warn_if_it_loses(result, baseline)

    primary, _, best, model_uri = max(candidates)
    logger.info("Best family on validation: %s", best)
    registry.promote(model_uri, primary, FEATURES)
    return write_report(settings, "models_validation", "validation", baseline, results)
