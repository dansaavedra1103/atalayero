import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from atalayero.models.data import FEATURES
from atalayero.monitoring.drift import day_kind, detect_drift, psi
from atalayero.settings import Settings


def _numeric(values: np.ndarray) -> pd.Series:
    return pd.Series(values, name="amount_usd")


def test_identical_distributions_do_not_drift() -> None:
    rng = np.random.default_rng(0)
    reference = _numeric(rng.lognormal(3, 1, 20_000))
    current = _numeric(rng.lognormal(3, 1, 20_000))

    assert psi(reference, current, bins=10) < 0.01


def test_a_shifted_distribution_drifts() -> None:
    rng = np.random.default_rng(0)
    reference = _numeric(rng.lognormal(3, 1, 20_000))

    assert psi(reference, _numeric(rng.lognormal(4, 1, 20_000)), bins=10) > 0.25


def test_missing_values_have_a_bin_of_their_own() -> None:
    reference = _numeric(np.array([1.0, 2.0, 3.0, np.nan] * 250))
    current = _numeric(np.array([1.0, 2.0, np.nan, np.nan] * 250))

    assert psi(reference, current, bins=4) > 0.1


def test_a_constant_reference_still_compares() -> None:
    reference = _numeric(np.zeros(1000))

    assert psi(reference, _numeric(np.zeros(1000)), bins=10) == pytest.approx(0)
    assert psi(reference, _numeric(np.ones(1000)), bins=10) > 1


def test_categories_compare_by_share_and_new_ones_count() -> None:
    reference = pd.Series(["ach"] * 900 + ["wire"] * 100, name="payment_format")
    same = pd.Series(["ach"] * 450 + ["wire"] * 50, name="payment_format")
    new = pd.Series(["ach"] * 700 + ["bitcoin"] * 300, name="payment_format")

    assert psi(reference, same, bins=10) == pytest.approx(0)
    assert psi(reference, new, bins=10) > 0.25


def test_weekends_are_compared_with_weekends() -> None:
    assert [day_kind(date(2022, 9, d)) for d in (2, 3, 4, 5)] == [
        "weekday",
        "weekend",
        "weekend",
        "weekday",
    ]


def test_drift_report(model_settings: Settings) -> None:
    report = detect_drift(model_settings, "validation")

    [day] = report.days  # validation is 8 Sep in the fixture, a Thursday
    assert day.day == date(2022, 9, 8)
    assert day.compared_with == "weekday"
    assert sorted(f.feature for f in day.features) == sorted(FEATURES)
    assert [f.psi for f in day.features] == sorted((f.psi for f in day.features), reverse=True)
    assert day.rule_alerts == 3  # the three R01 alerts of the fixture
    assert day.rule_alerts_reference == 0
    assert report.drift_detected == (day.significant > 0)
    path = model_settings.evaluation.reports_dir / "drift_validation.json"
    assert json.loads(path.read_text())["split"] == "validation"
