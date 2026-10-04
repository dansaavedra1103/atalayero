"""CLI: python -m atalayero.ingestion {download,load,load-sample,sample,fx-rates}."""

import argparse
import logging
from pathlib import Path

from atalayero.ingestion.download import download_dataset
from atalayero.ingestion.fx_rates import write_fx_seed
from atalayero.ingestion.load_batch import load_batch
from atalayero.ingestion.sample import write_sample
from atalayero.settings import Settings

FIXTURE_CSV = Path("tests/fixtures/hi_small_sample.csv")
FIXTURE_PATTERNS = Path("tests/fixtures/hi_small_patterns_sample.txt")
FX_SEED = Path("dbt/seeds/fx_rates_usd.csv")

COMMANDS = {
    "download": "fetch and verify the dataset",
    "load": "load the downloaded files into the DuckDB raw schema",
    "load-sample": f"load {FIXTURE_CSV} and {FIXTURE_PATTERNS} instead",
    "sample": f"regenerate {FIXTURE_CSV} and {FIXTURE_PATTERNS} from the downloaded files",
    "fx-rates": f"regenerate {FX_SEED} from raw.transactions",
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.ingestion")
    parser.add_argument(
        "command",
        choices=list(COMMANDS),
        help="; ".join(f"{name}: {text}" for name, text in COMMANDS.items()),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    settings = Settings()
    if args.command == "download":
        download_dataset(settings)
    elif args.command == "load":
        load_batch(settings)
    elif args.command == "load-sample":
        load_batch(settings, FIXTURE_CSV, FIXTURE_PATTERNS)
    elif args.command == "sample":
        write_sample(
            settings.raw_dir / settings.dataset.transactions.name,
            settings.raw_dir / settings.dataset.patterns.name,
            FIXTURE_CSV,
            FIXTURE_PATTERNS,
        )
    else:
        write_fx_seed(settings.duckdb_path, FX_SEED)


if __name__ == "__main__":
    main()
