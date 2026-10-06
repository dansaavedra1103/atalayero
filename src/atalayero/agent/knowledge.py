"""Typology notes for the investigator, embedded and searched with FAISS (ADR-0017).

The notes in `knowledge_base/typologies/` are written for this project, in its own words. Each
`## ` section of a note becomes a chunk, prefixed with the note's title so that it stands on its
own. A chunk carries its note's typology, except an "Easily confused with" section: it describes
legitimate activity, so it carries `none`, and its note says what it is contrasted with.

An embedding model served by Ollama turns chunks and queries into vectors, and FAISS returns the
chunks closest to a query by cosine similarity (the inner product of normalised vectors).
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from pydantic import BaseModel, ConfigDict

from atalayero.settings import Settings

logger = logging.getLogger(__name__)

Embedder = Callable[[Sequence[str]], np.ndarray]
MAX_RESULTS = 8
CPU = {"num_gpu": 0}  # Ollama option: no layer on the GPU
INDEX, CHUNKS, META = "typologies.faiss", "chunks.json", "meta.json"
LOOKALIKES = "easily confused"  # sections about legitimate activity that resembles the typology
_TITLE = re.compile(r"^# (?P<title>.+?) \(`(?P<typology>\w+)`\)\s*$")


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    note: str  # the file name, without extension
    title: str
    typology: str  # the report typology the section describes
    section: str
    text: str

    def document(self) -> str:
        """What gets embedded: the section with its note's title."""
        return f"{self.title}. {self.section}.\n{self.text}"


def read_notes(notes_dir: Path) -> list[Chunk]:
    """Every `## ` section of every note, in file order. A note starts with
    `# Title (`typology`)`."""
    chunks = []
    for path in sorted(notes_dir.glob("*.md")):
        head, *sections = path.read_text().split("\n## ")
        match = _TITLE.match(head.splitlines()[0])
        if match is None:
            raise ValueError(f"{path} must start with '# Title (`typology`)'")
        for section in sections:
            name, _, body = section.partition("\n")
            lookalike = name.strip().lower().startswith(LOOKALIKES)
            chunks.append(
                Chunk(
                    note=path.stem,
                    title=match["title"],
                    typology="none" if lookalike else match["typology"],
                    section=name.strip(),
                    text=body.strip(),
                )
            )
    return chunks


def _normalised(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype="float32")
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def ollama_embedder(settings: Settings) -> Embedder:
    """The embedding model of `knowledge.embedding_model`, served by Ollama."""
    import ollama

    client = ollama.Client(host=settings.ollama.url)
    model = settings.knowledge.embedding_model

    def embed(texts: Sequence[str]) -> np.ndarray:
        # On the CPU: as fast for a query, and it leaves the GPU to the language model.
        response = client.embed(model=model, input=list(texts), options=CPU)
        return np.asarray(response.embeddings)

    return embed


def build_index(settings: Settings, embedder: Embedder | None = None, digest: str = "") -> Path:
    """Embed every chunk of the notes and write the FAISS index, the chunks and what made them
    to `knowledge.index_dir`. Without an embedder, use Ollama's, at its pinned digest."""
    model = settings.knowledge.embedding_model
    if embedder is None:
        from atalayero.agent.llm import installed_digest

        digest = installed_digest(settings, model)
        pinned = settings.ollama.models.get(model)
        if pinned is not None and pinned != digest:
            raise ValueError(f"{model} is at {digest}, not at its pinned {pinned}: run `make llm`")
        embedder = ollama_embedder(settings)
    chunks = read_notes(settings.knowledge.notes_dir)
    vectors = _normalised(embedder([chunk.document() for chunk in chunks]))
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    directory = settings.knowledge.index_dir
    directory.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(directory / INDEX))
    (directory / CHUNKS).write_text(json.dumps([c.model_dump() for c in chunks], indent=1))
    notes = hashlib.sha256()
    for path in sorted(settings.knowledge.notes_dir.glob("*.md")):
        notes.update(path.read_bytes())
    meta = {
        "embedding_model": model,
        "digest": digest,
        "dimensions": int(vectors.shape[1]),
        "chunks": len(chunks),
        "notes_sha256": notes.hexdigest(),
    }
    (directory / META).write_text(json.dumps(meta, indent=1))
    logger.info(
        "Indexed %d sections of %d notes with %s",
        len(chunks),
        len(set(c.note for c in chunks)),
        model,
    )
    return directory / INDEX


class Knowledge:
    """The typology index, loaded once and searched by meaning."""

    def __init__(self, settings: Settings, embedder: Embedder | None = None) -> None:
        directory = settings.knowledge.index_dir
        if not (directory / INDEX).exists():
            raise FileNotFoundError(f"no typology index in {directory}: run `make knowledge`")
        meta = json.loads((directory / META).read_text())
        if meta["embedding_model"] != settings.knowledge.embedding_model:
            raise ValueError(
                f"the index was built with {meta['embedding_model']}, not "
                f"{settings.knowledge.embedding_model}: run `make knowledge`"
            )
        self.index = faiss.read_index(str(directory / INDEX))
        self.chunks = [Chunk(**c) for c in json.loads((directory / CHUNKS).read_text())]
        self.embedder = embedder or ollama_embedder(settings)

    def search(self, query: str, k: int) -> list[dict[str, Any]]:
        """The `k` sections closest to `query` (at most MAX_RESULTS), most similar first."""
        k = min(max(k, 1), MAX_RESULTS, len(self.chunks))
        similarities, positions = self.index.search(_normalised(self.embedder([query])), k)
        return [
            {
                "note": self.chunks[i].note,
                "typology": self.chunks[i].typology,
                "section": self.chunks[i].section,
                "text": self.chunks[i].text,
                "similarity": round(float(s), 4),
            }
            for s, i in zip(similarities[0], positions[0], strict=True)
        ]
