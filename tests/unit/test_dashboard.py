import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from atalayero.batch.serving import publish
from atalayero.settings import Settings

APP = str(Path(__file__).parents[2] / "dashboard" / "app.py")
BuildKpis = Callable[[Settings, Path], None]


@pytest.fixture(autouse=True)
def restore_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Streamlit installs the app as `__main__` and leaves it there; spawned workers of later
    tests (the graph features) would then run the dashboard as their main module."""
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])


@pytest.fixture(scope="module")
def published(
    replayed_once: Settings, tmp_path_factory: pytest.TempPathFactory, build_kpis: BuildKpis
) -> Settings:
    build_kpis(replayed_once, tmp_path_factory.mktemp("dbt"))
    publish(replayed_once)
    return replayed_once


@pytest.fixture
def app(published: Settings, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    """The dashboard, reading the published fixture through its own `Settings()`."""
    monkeypatch.setenv("ATALAYERO_BATCH__SERVING_PATH", str(published.batch.serving_path))
    monkeypatch.setenv("ATALAYERO_AGENT__INVESTIGATIONS_DIR", str(published.data_dir / "none"))
    return AppTest.from_file(APP, default_timeout=60)


def test_the_dashboard_shows_the_test_days_first(app: AppTest) -> None:
    app.run()
    assert not app.exception
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["Days"] == "1"  # the fixture's single test day
    assert set(metrics) == {
        "Days",
        "Alerts a day",
        "Alerts with no laundering",
        "Laundering detected",
    }
    assert "Synthetic data only" in app.caption[0].value
    assert len(app.get("vega_lite_chart")) == 3  # queue by source, shares by day, typologies
    assert len(app.dataframe) >= 2  # the rules and the alert queue


def test_choosing_phases_pools_their_days(app: AppTest) -> None:
    app.run()
    app.button_group[0].set_value(["validation", "test"]).run()
    assert not app.exception
    assert {m.label: m.value for m in app.metric}["Days"] == "2"
    app.button_group[0].set_value([]).run()
    assert "Choose at least one phase" in app.info[0].value


def test_the_dashboard_waits_for_published_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ATALAYERO_BATCH__SERVING_PATH", str(tmp_path / "none.duckdb"))
    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    assert not app.exception
    assert "No data published yet" in app.info[0].value
