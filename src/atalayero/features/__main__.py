"""CLI: python -m atalayero.features build."""

import argparse
import logging

from atalayero.features.graph import build_graph_features
from atalayero.features.motifs import build_motif_features
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
    settings = Settings()
    build_tabular_features(settings)
    build_graph_features(settings)
    build_motif_features(settings)


if __name__ == "__main__":
    main()
