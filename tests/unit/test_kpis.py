import gc
import os
import subprocess
import sys
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
    rule_kpis,
    typology_kpis,
)
from atalayero.settings import Settings


def _build_kpis(settings: Settings, tmp_path: Path) -> None:
    """`dbt build --select tag:batch` on the fixture's warehouse and batch files, in its own
    process as in production: DuckDB lets one process open a file in one database only."""
    gc.collect()  # connections the model fixtures left open would hold a lock on the file
    env = {
        **os.environ,
        "ATALAYERO_DUCKDB_PATH": str(settings.duckdb_path),
        "ATALAYERO_BATCH__DIR": str(settings.batch.dir),
    }
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "dbt"),
            "build",
            "--project-dir",
            "dbt",
            "--profiles-dir",
            "dbt",
            "--select",
            "tag:batch",
            "--target-path",
            str(tmp_path / "dbt-target"),
            "--log-path",
            str(tmp_path / "dbt-logs"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:]


@pytest.fixture
def with_kpis(replayed: Settings, tmp_path: Path) -> Settings:
    _build_kpis(replayed, tmp_path)
    publish(replayed)
    return replayed


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
    fitted_settings: Settings, tmp_path: Path
) -> None:
    empty = fitted_settings.model_copy(
        update={"batch": fitted_settings.batch.model_copy(update={"dir": tmp_path / "none"})}
    )
    _build_kpis(empty, tmp_path)
    with duckdb.connect(str(empty.duckdb_path), read_only=True) as con:
        counts = [con.execute(f"SELECT count(*) FROM marts.{t}").fetchone()[0] for t in KPI_TABLES]
    assert counts == [0, 0, 0]
