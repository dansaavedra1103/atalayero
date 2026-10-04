.PHONY: setup check

setup:
	uv sync
	uv run pre-commit install

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run pytest
