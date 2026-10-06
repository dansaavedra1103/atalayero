"""CLI: python -m atalayero.agent pull | knowledge."""

import argparse
import logging

from atalayero.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.agent")
    parser.add_argument(
        "command",
        choices=["pull", "knowledge"],
        help="pull the local models into Ollama and check their digests; or build the typology "
        "index",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if args.command == "pull":
        from atalayero.agent.llm import pull_models

        pull_models(settings)
    else:
        from atalayero.agent.knowledge import build_index

        build_index(settings)


if __name__ == "__main__":
    main()
