"""CLI: python -m atalayero.monitoring drift [--split validation|test]."""

import argparse
import logging

from atalayero.monitoring.drift import detect_drift
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.monitoring")
    parser.add_argument("command", choices=["drift"], help="compare each day of a split with train")
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    detect_drift(Settings(), args.split)


if __name__ == "__main__":
    main()
