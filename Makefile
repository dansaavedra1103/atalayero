.PHONY: setup check ingest fixture

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
