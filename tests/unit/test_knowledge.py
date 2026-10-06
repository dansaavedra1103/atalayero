import hashlib
import re
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from atalayero.agent.knowledge import Knowledge, build_index, read_notes
from atalayero.agent.tools import ToolBox
from atalayero.schemas import TYPOLOGIES
from atalayero.settings import Settings

NOTES = Path(__file__).parents[2] / "knowledge_base" / "typologies"


def fake_embedder(texts: Sequence[str]) -> np.ndarray:
    """Bag of words hashed into 512 dimensions: similar wording, similar vectors."""
    vectors = np.zeros((len(texts), 512), dtype="float32")
    for row, text in enumerate(texts):
        for word in re.findall(r"[a-z]+", text.lower()):
            vectors[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % 512] += 1
    return vectors + 1e-6  # no all-zero vector


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings()
    return base.model_copy(
        update={
            "knowledge": base.knowledge.model_copy(
                update={"notes_dir": NOTES, "index_dir": tmp_path / "knowledge"}
            )
        }
    )


def test_the_notes_cover_every_answer_an_investigator_can_give() -> None:
    chunks = read_notes(NOTES)

    typologies = {c.typology for c in chunks}
    assert typologies == {*TYPOLOGIES, "unclassified", "none"}
    for note in {c.note for c in chunks}:
        sections = [c.section for c in chunks if c.note == note]
        assert "Sources" in sections, f"{note} cites no source"
    lookalikes = [c for c in chunks if c.section == "Easily confused with"]
    assert len(lookalikes) == 8 and {c.typology for c in lookalikes} == {"none"}
    assert all(c.text for c in chunks)


def test_a_note_must_name_its_typology(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text("# No typology here\n\n## Section\nText\n")

    with pytest.raises(ValueError, match="must start with"):
        read_notes(tmp_path)


def test_search_returns_the_closest_sections(settings: Settings) -> None:
    build_index(settings, fake_embedder, digest="fake")
    knowledge = Knowledge(settings, fake_embedder)

    results = knowledge.search("money comes back to the same account in a cycle", k=3)
    everything = knowledge.search("account", k=100)

    assert results[0]["typology"] == "cycle"
    similarities = [r["similarity"] for r in results]
    assert similarities == sorted(similarities, reverse=True)
    assert len(everything) == 8  # capped


def test_an_index_from_another_model_is_refused(settings: Settings) -> None:
    build_index(settings, fake_embedder, digest="fake")
    other = settings.model_copy(
        update={"knowledge": settings.knowledge.model_copy(update={"embedding_model": "other"})}
    )

    with pytest.raises(ValueError, match="run `make knowledge`"):
        Knowledge(other, fake_embedder)


def test_the_toolbox_asks_for_the_index_when_it_is_missing(settings: Settings) -> None:
    toolbox = ToolBox(settings, [])

    with pytest.raises(ValueError, match="make knowledge"):
        toolbox.search_typologies("fan-in")

    build_index(settings, fake_embedder, digest="fake")
    found = ToolBox(settings, [], Knowledge(settings, fake_embedder)).search_typologies("fan in")
    assert len(found["results"]) == settings.knowledge.top_k
