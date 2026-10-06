"""CLI: python -m atalayero.models evaluate [--split validation|test] | tune | train | holdout."""

import argparse
import logging

from atalayero.models.evaluate import evaluate_rules_only
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.models")
    parser.add_argument(
        "command",
        choices=["evaluate", "tune", "train", "holdout"],
        help="evaluate the rules-only baseline on a split; tune the hyperparameters on "
        "validation; train the models and compare them with the rules on validation; or refit "
        "them on train and validation and compare them with the rules on test, once",
    )
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    if args.command == "evaluate":
        evaluate_rules_only(settings, args.split)
    elif args.command == "tune":
        from atalayero.models.tune import tune  # MLflow and Optuna are slow to import

        tune(settings)
    elif args.command == "train":
        from atalayero.models.train import train_and_evaluate

        train_and_evaluate(settings)
    else:
        from atalayero.models.holdout import run_holdout

        run_holdout(settings)


if __name__ == "__main__":
    main()
