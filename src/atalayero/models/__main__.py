"""CLI: python -m atalayero.models evaluate [--split validation|test] | train."""

import argparse
import logging

from atalayero.models.evaluate import evaluate_rules_only
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.models")
    parser.add_argument(
        "command",
        choices=["evaluate", "train"],
        help="evaluate the rules-only baseline on a split, or train the models and compare them "
        "with the rules on validation",
    )
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    if args.command == "evaluate":
        evaluate_rules_only(settings, args.split)
    else:
        from atalayero.models.train import train_and_evaluate  # MLflow is slow to import

        train_and_evaluate(settings)


if __name__ == "__main__":
    main()
