"""CLI: python -m atalayero.agent pull | knowledge | investigate --alert <alert ID>."""

import argparse
import asyncio
import logging

from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.agent")
    parser.add_argument(
        "command",
        choices=["pull", "knowledge", "investigate"],
        help="pull the local models into Ollama and check their digests; build the typology "
        "index; or investigate one alert of the case sets",
    )
    parser.add_argument("--alert", help="the alert ID to investigate")
    args = parser.parse_args()
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
    else:
        investigate(settings, args.alert, parser)


def investigate(settings: Settings, alert_id: str | None, parser: argparse.ArgumentParser) -> None:
    from atalayero.agent.run import investigate_alerts, write_investigation
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
    path = write_investigation(settings, result)
    report = result.report.model_dump_json(indent=1) if result.report else None
    logging.info(
        "%s in %.0f s, %d tool calls, %d model calls; grounded: %s; written to %s\n%s",
        alert_id,
        result.seconds,
        result.steps,
        result.llm_calls,
        result.grounding.grounded if result.grounding else None,
        path,
        report or result.error,
    )


if __name__ == "__main__":
    main()
