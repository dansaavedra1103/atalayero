from collections.abc import Callable
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from atalayero.batch.day import manifests
from atalayero.batch.serving import KPI_TABLES, publish
from atalayero.ingestion.source import sql_literal
from atalayero.monitoring.kpis import (
    ServingUnavailableError,
    daily_kpis,
    drift_days,
    queue_sources,
    rule_kpis,
    rule_totals,
    summary,
    typology_kpis,
    typology_totals,
)
from atalayero.settings import Settings

BuildKpis = Callable[[Settings, Path], None]


@pytest.fixture(scope="module")
def with_kpis(
    replayed_once: Settings, tmp_path_factory: pytest.TempPathFactory, build_kpis: BuildKpis
) -> Settings:
    """The replayed fixture with its KPIs built and published, once: these tests only read."""
    build_kpis(replayed_once, tmp_path_factory.mktemp("dbt"))
    publish(replayed_once)
    return replayed_once


def _expected(settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The queue and the laundering legs, from the batch's files and the labels, in pandas."""
    days = sql_literal(str(settings.batch.dir / "days" / "*" / "alerts.parquet"))
    queue = duckdb.sql(f"SELECT * FROM read_parquet({days})").df()
    with duckdb.connect(str(settings.duckdb_path), read_only=True) as con:
        laundering = con.execute(
            """
            SELECT t.transaction_id, t.transacted_at::date AS day,
                t.sender_account_key, t.receiver_account_key
            FROM marts.fct_transactions AS t
            JOIN marts.fct_laundering_labels AS l USING (transaction_id)
            WHERE l.is_laundering
            """
        ).df()
    return queue, laundering


def test_daily_kpis_count_the_queue_and_what_it_covered(with_kpis: Settings) -> None:
    queue, laundering = _expected(with_kpis)
    alerted = set(zip(queue["day"], queue["account_key"], strict=True))
    kpis = daily_kpis(with_kpis).set_index("day")
    assert list(kpis["phase"]) == ["warm-up", "train", "validation", "test"]
    assert pd.isna(kpis.iloc[0]["alerts"]) and pd.isna(kpis.iloc[0]["detection_rate"])
    for day, row in kpis.iloc[1:].iterrows():
        today = queue[queue["day"] == day]
        assert row["alerts"] == len(today)
        assert row["model_alerts"] == sum("model" in s for s in today["sources"])
        assert row["rule_alerts"] == sum(any(x != "model" for x in s) for s in today["sources"])
        legs = laundering[laundering["day"] == day]
        with_laundering = {
            a
            for a in today["account_key"]
            if a in set(legs["sender_account_key"]) | set(legs["receiver_account_key"])
        }
        assert row["alerts_with_laundering"] == len(with_laundering)
        assert row["false_positive_share"] == pytest.approx(1 - len(with_laundering) / len(today))
        detected = sum(
            (day, s) in alerted or (day, r) in alerted
            for s, r in zip(legs["sender_account_key"], legs["receiver_account_key"], strict=True)
        )
        assert row["laundering_transactions"] == len(legs)
        assert row["laundering_detected"] == detected
        assert row["detection_rate"] == pytest.approx(detected / len(legs))


def test_rule_and_typology_kpis_add_up(with_kpis: Settings) -> None:
    complete = {m.day: m for m in manifests(with_kpis)}
    rules = rule_kpis(with_kpis)
    per_day = rules.groupby("day")["alerts"].sum()
    assert {pd.Timestamp(d).date(): int(n) for d, n in per_day.items()} == {
        day: m.rule_alerts for day, m in complete.items() if m.rule_alerts
    }
    assert (rules["account_days_in_queue"] <= rules["account_days"]).all()
    typologies = typology_kpis(with_kpis, ["validation", "test"])
    daily = daily_kpis(with_kpis, ["validation", "test"]).set_index("day")
    by_day = typologies.groupby("day")[["laundering_transactions", "laundering_detected"]].sum()
    pd.testing.assert_frame_equal(
        by_day, daily[["laundering_transactions", "laundering_detected"]], check_dtype=False
    )
    assert set(typologies["typology"]) == {"untyped"}  # the fixture's laundering is untyped


def test_drift_days_are_validation_and_test_days(with_kpis: Settings) -> None:
    drift = drift_days(with_kpis)
    assert list(drift["phase"]) == ["validation", "test"]
    assert all(len(f) == 5 for f in drift["top_features"])


def test_phases_must_be_known(with_kpis: Settings) -> None:
    with pytest.raises(ValueError, match="unknown phases"):
        daily_kpis(with_kpis, ["holdout"])


def test_kpis_wait_for_the_serving_database(replayed: Settings) -> None:
    with pytest.raises(ServingUnavailableError, match="make replay"):
        daily_kpis(replayed)
    publish(replayed)  # before dbt built the KPIs: the serving database lacks them
    with duckdb.connect(str(replayed.batch.serving_path), read_only=True) as con:
        tables = {name for (name,) in con.execute("SHOW TABLES").fetchall()}
    assert not tables & set(KPI_TABLES)
    with pytest.raises(ServingUnavailableError, match="no such table"):
        rule_kpis(replayed)


def test_the_kpi_models_build_before_the_batch_has_run(
    fitted_settings: Settings, tmp_path: Path, build_kpis: BuildKpis
) -> None:
    empty = fitted_settings.model_copy(
        update={"batch": fitted_settings.batch.model_copy(update={"dir": tmp_path / "none"})}
    )
    build_kpis(empty, tmp_path)
    with duckdb.connect(str(empty.duckdb_path), read_only=True) as con:
        counts = [con.execute(f"SELECT count(*) FROM marts.{t}").fetchone()[0] for t in KPI_TABLES]
    assert counts == [0, 0, 0]


def test_rates_over_several_days_are_pooled() -> None:
    daily = pd.DataFrame(
        {
            "day": pd.to_datetime(["2022-09-01", "2022-09-02", "2022-09-03"]),
            "phase": ["warm-up", "test", "test"],
            "alerts": [None, 10, 30],
            "model_alerts": [None, 8, 20],
            "rule_alerts": [None, 4, 15],
            "alerts_with_laundering": [None, 5, 3],
            "laundering_transactions": [9, 20, 0],
            "laundering_detected": [None, 4, 0],
        }
    )
    assert summary(daily) == {
        "days": 2,  # the warm-up day has no queue
        "alerts_per_day": 20.0,
        "false_positive_share": pytest.approx(1 - 8 / 40),
        "detection_rate": pytest.approx(4 / 20),
    }
    sources = queue_sources(daily).pivot(index="day", columns="source", values="alerts")
    assert sources.loc["2022-09-02"].to_dict() == {"both": 2, "model_only": 6, "rules_only": 2}
    assert (sources.sum(axis=1).to_numpy() == [10, 30]).all()
    empty = summary(daily.iloc[:1])
    assert empty["days"] == 0 and empty["detection_rate"] is None


def test_rule_and_typology_totals_pool_their_days() -> None:
    rules = pd.DataFrame(
        {
            "rule_id": ["R01", "R01", "R02"],
            "alerts": [3, 1, 2],
            "account_days": [2, 2, 2],
            "account_days_with_laundering": [1, 0, 2],
            "account_days_in_queue": [2, 1, 0],
        }
    )
    totals = rule_totals(rules).set_index("rule_id")
    assert totals.loc["R01", "hit_rate"] == pytest.approx(1 / 4)
    assert totals.loc["R01", "in_queue"] == pytest.approx(3 / 4)
    assert totals.loc["R02", "in_queue"] == 0
    typologies = pd.DataFrame(
        {
            "typology": ["cycle", "cycle", "untyped"],
            "laundering_transactions": [4, 6, 10],
            "laundering_detected": [2, 3, 0],
        }
    )
    totals = typology_totals(typologies)
    assert list(totals["typology"]) == ["cycle", "untyped"]
    assert list(totals["detection_rate"]) == [0.5, 0.0]
