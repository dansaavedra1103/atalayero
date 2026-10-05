from pathlib import Path

import pytest

from atalayero.settings import Settings

REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)


def test_reads_settings_yaml() -> None:
    settings = Settings()

    assert settings.raw_dir == settings.data_dir / "raw"
    assert settings.dataset.transactions.name == "HI-Small_Trans.csv"
    assert len(settings.dataset.transactions.sha256) == 64


def test_splits_follow_adr_0005() -> None:
    splits = Settings().splits

    assert [d.isoformat() for d in (splits.train_end, splits.validation_end, splits.test_end)] == [
        "2022-09-07T00:00:00",
        "2022-09-09T00:00:00",
        "2022-09-11T00:00:00",
    ]


def test_splits_must_be_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATALAYERO_SPLITS__VALIDATION_END", "2022-09-12")

    with pytest.raises(ValueError, match="train_end < validation_end < test_end"):
        Settings()


def test_url_pins_the_dataset_version() -> None:
    dataset = Settings().dataset

    url = dataset.url(dataset.patterns)

    assert url.startswith(dataset.base_url + "/HI-Small_Patterns.txt")
    assert url.endswith(f"?datasetVersionNumber={dataset.version}")


def test_environment_overrides_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ATALAYERO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ATALAYERO_DATASET__VERSION", "99")

    settings = Settings()

    assert settings.data_dir == tmp_path
    assert settings.dataset.version == 99
    assert settings.dataset.transactions.name == "HI-Small_Trans.csv"
