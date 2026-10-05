"""CLI: python -m atalayero.models evaluate [--split validation|test]."""

import argparse
import logging

from atalayero.models.evaluate import evaluate_rules_only
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.models")
    parser.add_argument(
        "command", choices=["evaluate"], help="evaluate the rules-only baseline on a split"
    )
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    evaluate_rules_only(Settings(), args.split)


if __name__ == "__main__":
    main()
