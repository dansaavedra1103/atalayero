"""CLI: python -m atalayero.features build."""

import argparse
import logging

from atalayero.features.tabular import build_tabular_features
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.features")
    parser.add_argument(
        "command", choices=["build"], help="build the features of every transaction"
    )
    parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    build_tabular_features(Settings())


if __name__ == "__main__":
    main()
