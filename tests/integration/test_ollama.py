"""Needs Ollama with the embedding model (`make up`, `make llm`)."""

from pathlib import Path

import pytest

from atalayero.agent.knowledge import Knowledge, build_index
from atalayero.settings import Settings

pytestmark = pytest.mark.integration


def test_ollama_embeddings_find_the_typology_described(tmp_path: Path) -> None:
    base = Settings()
    settings = base.model_copy(
        update={"knowledge": base.knowledge.model_copy(update={"index_dir": tmp_path})}
    )
    build_index(settings)
    knowledge = Knowledge(settings)

    cases = {
        "money leaves the account and returns to it through two intermediaries": "cycle",
        "dozens of small transfers from different senders into one account": "fan_in",
        "a company pays the same employees every month": "none",
    }
    for query, typology in cases.items():
        found = [r["typology"] for r in knowledge.search(query, k=3)]
        assert typology in found, f"{query!r} found {found}"
