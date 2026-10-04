"""CLI: python -m atalayero.streaming replay [--limit N]."""

import argparse
import logging

from atalayero.settings import Settings
from atalayero.streaming.replay import replay


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.streaming")
    parser.add_argument(
        "command", choices=["replay"], help="replay the transactions from the start"
    )
    parser.add_argument("--limit", type=int, help="replay only the first N transactions")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    replay(Settings(), args.limit)


if __name__ == "__main__":
    main()
