"""CLI: python -m atalayero.batch day <YYYY-MM-DD> | replay [--first D] [--last D] | publish.

After `day` or `replay`, `dbt build --select tag:batch` builds the KPIs and `publish` moves a new
serving database into place (ADR-0020); `make daily` and `make replay` run all three."""

import argparse
import logging
from datetime import date

from atalayero.batch.day import Batch
from atalayero.batch.serving import publish
from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.batch")
    commands = parser.add_subparsers(dest="command", required=True)
    day = commands.add_parser("day", help="run one day of the simulation")
    day.add_argument("day", type=date.fromisoformat)
    replay = commands.add_parser("replay", help="run every day of the simulation, in order")
    replay.add_argument("--first", type=date.fromisoformat)
    replay.add_argument("--last", type=date.fromisoformat)
    commands.add_parser("publish", help="publish the serving database from the complete days")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    if args.command == "day":
        Batch(settings).run_day(args.day)
    elif args.command == "replay":
        Batch(settings).replay(args.first, args.last)
    else:
        publish(settings)


if __name__ == "__main__":
    main()
