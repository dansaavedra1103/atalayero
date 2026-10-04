import hashlib
from pathlib import Path

import pytest

from atalayero.ingestion.download import ChecksumError, download_file

CONTENT = b"Timestamp,From Bank\n2022/09/01 00:20,010\n"
CONTENT_SHA256 = hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
def source_url(tmp_path: Path) -> str:
    source = tmp_path / "source.csv"
    source.write_bytes(CONTENT)
    return source.as_uri()


def test_downloads_and_verifies(source_url: str, tmp_path: Path) -> None:
    dest = tmp_path / "raw" / "file.csv"

    download_file(source_url, dest, CONTENT_SHA256)

    assert dest.read_bytes() == CONTENT
    assert list(dest.parent.iterdir()) == [dest]


def test_checksum_mismatch_leaves_no_file(source_url: str, tmp_path: Path) -> None:
    dest = tmp_path / "raw" / "file.csv"

    with pytest.raises(ChecksumError):
        download_file(source_url, dest, "0" * 64)

    assert list(dest.parent.iterdir()) == []


def test_skips_download_when_file_is_already_verified(tmp_path: Path) -> None:
    dest = tmp_path / "file.csv"
    dest.write_bytes(CONTENT)
    unreachable = (tmp_path / "missing.csv").as_uri()  # would raise if it were fetched

    download_file(unreachable, dest, CONTENT_SHA256)

    assert dest.read_bytes() == CONTENT


def test_replaces_a_corrupt_file(source_url: str, tmp_path: Path) -> None:
    dest = tmp_path / "file.csv"
    dest.write_bytes(b"truncated")

    download_file(source_url, dest, CONTENT_SHA256)

    assert dest.read_bytes() == CONTENT
