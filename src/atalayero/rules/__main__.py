"""CLI: python -m atalayero.rules evaluate."""

import argparse
import logging

from atalayero.rules.batch import evaluate_rules
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.rules")
    parser.add_argument(
        "command", choices=["evaluate"], help="evaluate the rules over the batch data"
    )
    parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    evaluate_rules(Settings())


if __name__ == "__main__":
    main()
