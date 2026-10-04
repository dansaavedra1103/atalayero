from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_csv() -> Path:
    """1,000 rows of HI-Small_Trans.csv in the source format (see tests/fixtures/README.md)."""
    return FIXTURES_DIR / "hi_small_sample.csv"
