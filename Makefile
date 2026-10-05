DBT_BUILD := uv run dbt build --project-dir dbt --profiles-dir dbt
# The test fixtures, loaded into their own disposable DuckDB file.
SAMPLE_ENV := ATALAYERO_DATA_DIR=data/sample ATALAYERO_DUCKDB_PATH=data/sample/atalayero.duckdb
# MLflow prints a hint for coding agents on import; it is noise in the logs.
MLFLOW_ENV := MLFLOW_DISABLE_AGENT_HINT=1

.PHONY: setup check test-integration ingest fixture fx-rates dbt dbt-sample up down stream rules evaluate features tune train mlflow-ui

# LightGBM needs the system OpenMP runtime: sudo apt-get install libgomp1 (ADR-0009).
setup:
	uv sync
	uv run pre-commit install

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run pytest

test-integration:
	uv run pytest -m integration

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

up:
	docker compose up -d --wait

down:
	docker compose down

stream:
	uv run python -m atalayero.streaming replay

rules:
	uv run python -m atalayero.rules evaluate

evaluate:
	uv run python -m atalayero.models evaluate --split validation

features:
	uv run python -m atalayero.features build

tune:
	$(MLFLOW_ENV) uv run python -m atalayero.models tune

train:
	$(MLFLOW_ENV) uv run python -m atalayero.models train

mlflow-ui:
	$(MLFLOW_ENV) uv run mlflow ui --backend-store-uri sqlite:///data/mlflow/mlflow.db --port 5000
