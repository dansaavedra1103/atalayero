DBT_BUILD := uv run dbt build --project-dir dbt --profiles-dir dbt
# The test fixtures, loaded into their own disposable DuckDB file.
SAMPLE_ENV := ATALAYERO_DATA_DIR=data/sample ATALAYERO_DUCKDB_PATH=data/sample/atalayero.duckdb
# MLflow prints a hint for coding agents on import; it is noise in the logs.
MLFLOW_ENV := MLFLOW_DISABLE_AGENT_HINT=1

.PHONY: setup check test-integration ingest fixture fx-rates dbt dbt-sample up down stream rules evaluate features tune train mlflow-ui drift holdout pipeline notebook golden-set eval llm knowledge investigate daily replay

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

drift:
	uv run python -m atalayero.monitoring drift --split validation

# The single test run (ADR-0014): rules per rule, models refit on train + validation, drift.
holdout:
	uv run python -m atalayero.models evaluate --split test
	$(MLFLOW_ENV) uv run python -m atalayero.models holdout
	uv run python -m atalayero.monitoring drift --split test

# Everything from the download to the test run. Leaves out `tune` (config/models.yaml is the
# source of truth) and `stream` (needs Docker; `rules` gives the same alerts).
pipeline:
	$(MAKE) ingest
	$(MAKE) dbt
	$(MAKE) rules
	$(MAKE) features
	$(MAKE) evaluate
	$(MAKE) train
	$(MAKE) drift
	$(MAKE) holdout

# Executes the error analysis in place, outputs included (ADR-0014; needs `make holdout`).
notebook:
	uv run jupyter nbconvert --to notebook --execute --inplace notebooks/03_error_analysis.ipynb

# The case sets of the agent evals, sampled from the alert queues (ADR-0015; needs `make train`
# and `make holdout`). The golden set reads the test split.
golden-set:
	$(MLFLOW_ENV) uv run python -m atalayero.evals build

# A detector on a case set; the golden set is read once per reported result (ADR-0015).
DETECTOR ?= baseline
SET ?= dev
eval:
	$(MLFLOW_ENV) uv run python -m atalayero.evals run --detector $(DETECTOR) --set $(SET)

# The local models of config/settings.yaml, pulled into Ollama over HTTP and checked against their
# pinned digests (ADR-0017; needs `make up`).
llm:
	uv run python -m atalayero.agent pull

# The FAISS index of the typology notes, embedded by Ollama (ADR-0017; needs `make llm`).
knowledge:
	uv run python -m atalayero.agent knowledge

# One investigation by the agent, on an alert of the case sets (ADR-0018; needs `make knowledge`).
investigate:
	$(MLFLOW_ENV) uv run python -m atalayero.agent investigate --alert "$(ALERT)"

# The daily batch (ADR-0020): one day of the simulation, or every day in order; both publish the
# serving database the API and the dashboard read.
daily:
	@test -n "$(DAY)" || (echo "usage: make daily DAY=YYYY-MM-DD" && exit 1)
	$(MLFLOW_ENV) uv run python -m atalayero.batch day $(DAY)

replay:
	$(MLFLOW_ENV) uv run python -m atalayero.batch replay
