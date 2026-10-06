"""The local models, served by Ollama (ADR-0017): pulled over HTTP and pinned by digest, so that
embeddings and answers can be reproduced."""

import logging

import ollama

from atalayero.settings import Settings

logger = logging.getLogger(__name__)


def installed_digest(settings: Settings, model: str) -> str:
    """The digest of a model as Ollama has it; fails if it is not pulled."""
    for entry in ollama.Client(host=settings.ollama.url).list().models:
        if entry.model == model:
            return entry.digest
    raise LookupError(f"{model} is not in Ollama: run `make llm`")


def pull_models(settings: Settings) -> dict[str, str]:
    """Pull every model of `ollama.models` and check it against its pinned digest. Returns the
    digests; a model pinned to None is pulled and its digest logged, to be pinned."""
    client = ollama.Client(host=settings.ollama.url)
    digests = {}
    for model, pinned in settings.ollama.models.items():
        logger.info("Pulling %s", model)
        last = None
        for progress in client.pull(model, stream=True):
            if progress.status != last:
                logger.info("%s: %s", model, progress.status)
                last = progress.status
        digest = installed_digest(settings, model)
        if pinned is None:
            logger.warning("%s is not pinned; pin it in config/settings.yaml: %s", model, digest)
        elif digest != pinned:
            raise ValueError(f"{model} is at {digest}, not at its pinned {pinned}")
        digests[model] = digest
    return digests
