"""Download the dataset files over HTTPS and verify their SHA-256 checksums."""

import hashlib
import logging
import urllib.request
from pathlib import Path

from atalayero.settings import Settings

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 1024 * 1024


class ChecksumError(Exception):
    """A downloaded file does not match its expected SHA-256."""


def file_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def download_file(url: str, dest: Path, sha256: str) -> Path:
    """Download `url` to `dest` unless `dest` already exists with the expected checksum.

    The file is streamed to `<dest>.part` and only renamed to `dest` once its checksum matches,
    so `dest` never holds a partial or unverified file.
    """
    if dest.exists():
        if file_sha256(dest) == sha256:
            logger.info("%s already present, checksum OK", dest)
            return dest
        logger.warning("%s has an unexpected checksum, downloading it again", dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    logger.info("Downloading %s -> %s", url, dest)
    try:
        with urllib.request.urlopen(url, timeout=60) as response, part.open("wb") as out:
            while chunk := response.read(_CHUNK_BYTES):
                out.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise ChecksumError(
                f"{url}: expected SHA-256 {sha256}, got {digest.hexdigest()}. "
                "If the source published a new version, review it and update config/settings.yaml."
            )
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return dest


def download_dataset(settings: Settings) -> None:
    dataset = settings.dataset
    for file in (dataset.transactions, dataset.patterns):
        download_file(dataset.url(file), settings.raw_dir / file.name, file.sha256)
