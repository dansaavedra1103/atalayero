"""CLI: python -m atalayero.ingestion {download,load,sample}."""

import argparse
import logging
from pathlib import Path

from atalayero.ingestion.download import download_dataset
from atalayero.ingestion.load_batch import load_batch
from atalayero.ingestion.sample import write_sample
from atalayero.settings import Settings

FIXTURE_PATH = Path("tests/fixtures/hi_small_sample.csv")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atalayero.ingestion")
    parser.add_argument(
        "command",
        choices=["download", "load", "sample"],
        help="download: fetch and verify the dataset; load: CSV -> Parquet -> DuckDB; "
        f"sample: regenerate {FIXTURE_PATH}",
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
    else:
        write_sample(settings.raw_dir / settings.dataset.transactions.name, FIXTURE_PATH)


if __name__ == "__main__":
    main()
