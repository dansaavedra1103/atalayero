"""CLI: python -m atalayero.agent pull | knowledge | investigate (--alert <alert ID> | --day <day>
[--top <n>])."""

import argparse
import asyncio
import logging
from datetime import date

from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.agent")
    parser.add_argument(
        "command",
        choices=["pull", "knowledge", "investigate"],
        help="pull the local models into Ollama and check their digests; build the typology "
        "index; or investigate one alert of the case sets, or the top of a day's queue",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--alert", help="the ID of an alert of the case sets to investigate")
    target.add_argument(
        "--day", type=date.fromisoformat, help="a day of the batch (YYYY-MM-DD) to investigate"
    )
    parser.add_argument(
        "--top",
        type=int,
        help="how many alerts of the day's queue, by rank (default: batch.investigate_top)",
    )
    args = parser.parse_args()
    if args.top is not None and args.day is None:
        parser.error("--top goes with --day")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("faiss.loader").setLevel(logging.WARNING)  # it reports each CPU fallback
    settings = Settings()
    if args.command == "pull":
        from atalayero.agent.llm import pull_models

        pull_models(settings)
    elif args.command == "knowledge":
        from atalayero.agent.knowledge import build_index

        build_index(settings)
    elif args.day is not None:
        investigate_day(settings, args.day, args.top or settings.batch.investigate_top, parser)
    else:
        investigate(settings, args.alert, parser)


def investigate_day(
    settings: Settings, day: date, top: int, parser: argparse.ArgumentParser
) -> None:
    from atalayero.agent.run import investigate_day as run
    from atalayero.batch.day import MissingDaysError

    if top < 1:
        parser.error("--top takes a positive number")
    try:
        asyncio.run(run(settings, day, top))  # logs each investigation as it ends
    except (MissingDaysError, ValueError) as error:  # before any investigation starts
        parser.error(str(error))


def investigate(settings: Settings, alert_id: str | None, parser: argparse.ArgumentParser) -> None:
    from atalayero.agent.run import investigate_alerts, log_investigation, write_investigation
    from atalayero.evals.golden import load_alerts

    alerts = {
        a.alert_id: (case_set, a)
        for case_set in ("dev", "golden")
        for a in load_alerts(settings, case_set)
    }
    if alert_id not in alerts:
        parser.error("--alert takes the ID of an alert of evals/dev_alerts.jsonl or golden_alerts")
    case_set, alert = alerts[alert_id]
    if case_set == "golden":
        logging.warning("A golden-set alert: the golden set is for reported results (ADR-0015)")
    [result] = asyncio.run(investigate_alerts(settings, [alert]))
    log_investigation(result, write_investigation(settings, result))


if __name__ == "__main__":
    main()
