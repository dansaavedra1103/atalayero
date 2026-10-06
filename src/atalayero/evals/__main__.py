"""CLI: python -m atalayero.evals build [--set dev|golden|all] | run --detector baseline
--set dev|golden."""

import argparse
import logging

from atalayero.evals.golden import build_case_set
from atalayero.evals.run import run_eval
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.evals")
    parser.add_argument(
        "command",
        choices=["build", "run"],
        help="build the case sets from the alert queues; or run a detector on a case set",
    )
    parser.add_argument("--set", choices=["dev", "golden", "all"], default="all")
    parser.add_argument("--detector", choices=["baseline"], default="baseline")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    sets = ["dev", "golden"] if args.set == "all" else [args.set]
    if args.command == "build":
        for case_set in sets:
            build_case_set(settings, case_set)
    else:
        if args.set == "all":
            parser.error("run takes --set dev or --set golden")
        run_eval(settings, args.detector, args.set)


if __name__ == "__main__":
    main()
