DBT_BUILD := uv run dbt build --project-dir dbt --profiles-dir dbt
# The test fixtures, loaded into their own disposable DuckDB file.
SAMPLE_ENV := ATALAYERO_DATA_DIR=data/sample ATALAYERO_DUCKDB_PATH=data/sample/atalayero.duckdb

.PHONY: setup check ingest fixture fx-rates dbt dbt-sample

setup:
	uv sync
	uv run pre-commit install

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run pytest

ingest:
	uv run python -m atalayero.ingestion download
	uv run python -m atalayero.ingestion load

fixture:
	uv run python -m atalayero.ingestion sample

fx-rates:
	uv run python -m atalayero.ingestion fx-rates

dbt:
	$(DBT_BUILD)

dbt-sample:
	$(SAMPLE_ENV) uv run python -m atalayero.ingestion load-sample
	$(SAMPLE_ENV) $(DBT_BUILD)
