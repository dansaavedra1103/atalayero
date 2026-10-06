# ADR-0017: Local models with Ollama, and the typology knowledge base

- Status: accepted
- Date: 2026-10-06

## Context

- The investigator needs a language model and a `search_typologies` tool over notes about
  laundering typologies, indexed with FAISS (design.md §4). The notes must be the project's own,
  summarised from public sources.
- Everything runs locally and at no cost (CLAUDE.md rule 2). The machine has an RTX 3080 Laptop
  GPU with 16 GB of VRAM. Docker Desktop on Windows passes the GPU through to containers, but
  Claude Code sessions cannot reach the Docker CLI. They do reach published ports over TCP, as
  they reach Redpanda.
- Every pulled model is a moving target: a tag can be re-pushed with different weights.
- The case answers derive from the dataset's labels (ADR-0015). Notes that quoted statistics
  computed from those labels would leak them to the agent.

## Decision

1. **Ollama runs as a Docker Compose service**, image `ollama/ollama:0.35.1` (the latest stable
   release; 0.40.0 came out the same day). It reserves the NVIDIA GPU and keeps its weights in a
   volume. `make up` starts it with Redpanda, and the code talks to it over HTTP on
   `localhost:11434`.
2. **Models are pulled over HTTP and pinned by digest.** `config/settings.yaml` lists every model
   with its digest. `make llm` (`python -m atalayero.agent pull`) pulls each one and fails if its
   digest differs from the pinned one. A model pinned to null is pulled and its digest logged,
   so that it can be pinned. Pulling over HTTP needs no Docker CLI.
3. **The embedding model is `qwen3-embedding:0.6b`:** small, multilingual and widely used. The
   language models of the agent are chosen later, on the dev set (ADR-0015), among those that fit
   in 16 GB and support tool calling.
4. **Ten notes** in `knowledge_base/typologies/`, in English and in the project's own words:
   - one note for each of the eight typologies;
   - one for laundering without a clear typology (`unclassified`);
   - one for legitimate activity that looks suspicious (`none`), to help close false positives.

   Each note covers what the typology is, how it shows in transactions, what to check with the
   agent's tools, what it is easily confused with, and its sources:
   - the pattern definitions of AMLworld (Altman et al., arXiv:2306.16424);
   - the red flags of GAFILAT's *Informe de Tipologías Regionales de LA/FT 2025* (December 2025).
     That report forbids reproduction or translation without permission, so the notes summarise
     ideas and never quote it.

   **No note states a statistic computed from the dataset's labels.**
5. **Each `## ` section of a note is a chunk**, prefixed with the note's title. A chunk carries
   its note's typology, except an "Easily confused with" section: it describes legitimate
   activity, so it carries `none`, and its note says which typology it is contrasted with.
   - `make knowledge` (`python -m atalayero.agent knowledge`) embeds the chunks with Ollama and
     writes a FAISS `IndexFlatIP` over normalised vectors, so the similarity is cosine.
   - It also writes the chunks and what made them (model, digest, a checksum of the notes) to
     `data/knowledge/`, which can be rebuilt like the rest of `data/`.
   - It refuses a model whose digest differs from the pinned one, and the search refuses an
     index built with another model.
   - **Embeddings run on the CPU** (`num_gpu: 0`), for the index and for every query, so the
     vectors always come from the same device and the GPU stays free for the language model.
6. **`search_typologies(query, k)`** is the fifth tool of the MCP server (ADR-0016). It returns
   the closest sections, each with the typology it belongs to and its similarity, at most 8.
7. **New runtime dependencies:** `ollama`, the Python client, and `faiss-cpu`. With 50 chunks,
   exact search on the CPU is instant.

## Consequences

- **Pulling and indexing.**
  - `make llm` pulls the embedding model in about a minute. Its digest is now pinned
    (`ac6da0df…`).
  - `make knowledge` embeds the 50 sections of the 10 notes, at 1,024 dimensions, in about 19 s.
- **The CPU costs nothing for embeddings.**
  - On the GPU the model takes 2.4 GB of VRAM, and a query takes about 47 ms.
  - On the CPU a short query takes about 27 ms, and longer ones about 90 ms. The model takes
    no VRAM, which leaves the full 16 GB to the language model of the agent.
- **A check of ten descriptions**, one per answer an investigator can give, written for this
  test and not a benchmark:
  - the right typology comes first for 9 of them, and within the top three for all 10;
  - the miss is gather-scatter, found first as fan-in, which is its first half.

  The notes were not tuned to these queries.
- **Lookalike sections are labelled `none` because of this check.** Before, a query about a
  company paying the same employees every month found the right text (the "Easily confused
  with" sections about payroll) labelled with laundering typologies. That would have pointed
  the agent the wrong way.
- **The notes describe the dataset's eight typologies with the same definitions it uses.**
  Without that, the agent could not name them. They carry no statistic of the data, so they
  reveal nothing a case's answer depends on.
- **Docker for the user, HTTP for the session.** The user runs `make up` after any change to
  the compose file. Everything else (pulling, indexing, inference) goes over HTTP, from any
  session.
