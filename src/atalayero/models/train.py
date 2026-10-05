"""Fit each model family with its configured hyperparameters, evaluate it on validation against
the rules (ADR-0007) and register the best one in MLflow (ADR-0009)."""

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from atalayero.models.data import FEATURES, load_split
from atalayero.models.evaluate import AlertMetrics, EvaluationReport, SplitEvaluator
from atalayero.models.families import fit, load_models_config, transaction_scores
from atalayero.models.registry import Registry
from atalayero.rules.schema import load_rules
from atalayero.settings import Settings

logger = logging.getLogger(__name__)

CURVE_BUDGETS = (10, 25, 50, 75, 100, 150, 200, 300, 400, 600, 800, 1000)  # alerts per day


def _metrics(m: AlertMetrics) -> dict[str, float]:
    return {
        "validation/detection_without_hubs": m.laundering_without_hubs.rate,
        "validation/detection": m.laundering.rate,
        "validation/false_positive_share": m.false_positive_share,
        "validation/hub_alert_share": m.hub_alert_share,
        "validation/alerts_per_day": m.alerts_per_day,
        "validation/attempts": m.attempts.rate,
        "validation/patterned": m.patterned.rate,
        "validation/untyped": m.untyped.rate,
    }


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
    registry.log_run("rules-only", common, _metrics(baseline))

    results, curves, pr_auc = [baseline], {}, {}
    candidates: list[tuple[float, float, str, str]] = []
    for name, family in config.families.items():
        start = time.monotonic()
        model = fit(name, family.params, train, settings.models.seed)
        seconds = time.monotonic() - start
        evaluator.set_scores(
            validation.transaction_ids, transaction_scores(model, validation.features)
        )
        matched = evaluator.evaluate_scores(name, budget)
        fixed = [
            evaluator.evaluate_scores(f"{name} @ {n}/day", n) for n in settings.evaluation.budgets
        ]
        pr_auc[name] = evaluator.pr_auc()
        curves[name] = evaluator.curve(CURVE_BUDGETS)
        results += [matched, *fixed]
        logger.info(
            "%s: fit in %.0f s, PR-AUC %.4f, detection without hubs %.1f%% at %.1f alerts/day",
            name,
            seconds,
            pr_auc[name],
            100 * matched.laundering_without_hubs.rate,
            matched.alerts_per_day,
        )
        _, model_uri = registry.log_run(
            name,
            {**common, "family": name, **family.params},
            {**_metrics(matched), "validation/pr_auc": pr_auc[name], "fit_seconds": seconds},
            curves={
                "validation/curve/detection_without_hubs": [
                    (p.budget, p.detection_without_hubs) for p in curves[name]
                ],
                "validation/curve/false_positive_share": [
                    (p.budget, p.false_positive_share) for p in curves[name]
                ],
            },
            artifacts={
                "validation.json": EvaluationReport(
                    split="validation",
                    generated_at=datetime.now(UTC),
                    results=[matched, *fixed],
                    curves={name: curves[name]},
                    pr_auc={name: pr_auc[name]},
                ).model_dump_json(indent=2)
            },
            model=model,
        )
        assert model_uri is not None
        candidates.append((matched.laundering_without_hubs.rate, pr_auc[name], name, model_uri))
        if matched.laundering_without_hubs.rate <= baseline.laundering_without_hubs.rate:
            logger.warning(
                "%s does not beat the rules: %.1f%% against %.1f%% detection without hubs",
                name,
                100 * matched.laundering_without_hubs.rate,
                100 * baseline.laundering_without_hubs.rate,
            )

    primary, _, best, model_uri = max(candidates)
    logger.info("Best family on validation: %s", best)
    registry.promote(model_uri, primary)

    report = EvaluationReport(
        split="validation",
        generated_at=datetime.now(UTC),
        results=results,
        curves=curves,
        pr_auc=pr_auc,
    )
    settings.evaluation.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.evaluation.reports_dir / "models_validation.json"
    path.write_text(report.model_dump_json(indent=2))
    logger.info("Models against the rules on validation, written to %s:\n%s", path, report.table())
    return path
