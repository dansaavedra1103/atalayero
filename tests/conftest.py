from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_csv() -> Path:
    """Story sample of HI-Small_Trans.csv in the source format (see tests/fixtures/README.md)."""
    return FIXTURES_DIR / "hi_small_sample.csv"


@pytest.fixture
def sample_patterns() -> Path:
    """The laundering attempts of `sample_csv`, verbatim from HI-Small_Patterns.txt."""
    return FIXTURES_DIR / "hi_small_patterns_sample.txt"
