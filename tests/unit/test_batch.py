import random
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from atalayero.batch.day import Batch, MissingDaysError, day_dir, load_manifest, read_alerts
from atalayero.batch.serving import publish
from atalayero.evals.golden import queue_cases
from atalayero.features.graph import build_graph_features
from atalayero.features.motifs import build_motif_features
from atalayero.features.tabular import build_tabular_features
from atalayero.ingestion.source import sql_literal
from atalayero.models.data import load_split
from atalayero.models.evaluate import SplitEvaluator
from atalayero.models.families import transaction_scores
from atalayero.models.registry import Registry
from atalayero.monitoring.drift import detect_drift
from atalayero.rules.batch import evaluate_rules
from atalayero.rules.schema import load_rules
from atalayero.schemas import Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]
DAY = 24 * 60  # minutes
REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture
def dense_settings(
    make_tx: MakeTx,
    fct_transactions_db: WriteDb,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """Six days (5–10 Sep) of transactions among few accounts, dense enough for relay chains
    across days, every rule and their cooldowns. Never scored: training starts after them."""
    monkeypatch.chdir(REPO_ROOT)
    rng = random.Random(1)
    accounts = [f"00{i % 3}:{i}" for i in range(24)]
    transactions = [
        make_tx(
            i,
            rng.randrange(0, 6 * DAY - 540),
            sender=rng.choice(accounts),
            receiver=rng.choice(accounts),
            usd=rng.choice([5200.0, 6000.0, 6500.0, 9500.0, 800.0]),
        )
        for i in range(2400)
    ]
    # Bursts of payments from one account, for the velocity rule and its cooldown.
    burst = [(k, 30 * 60 + j, k % 24) for k in range(4) for j in range(14)]
    transactions += [
        make_tx(10_000 + n, minutes + k * DAY, sender=accounts[a], receiver=accounts[(a + n) % 24])
        for n, (k, minutes, a) in enumerate(burst)
    ]
    base = Settings()
    return base.model_copy(
        update={
            "duckdb_path": fct_transactions_db(transactions),
            "features_dir": tmp_path / "features",
            "rule_alerts_dir": tmp_path / "rule_alerts",
            "data_dir": tmp_path,
            "graph_workers": 1,
            "batch": base.batch.model_copy(
                update={"dir": tmp_path / "batch", "serving_path": tmp_path / "serving.duckdb"}
            ),
            "models": base.models.model_copy(update={"train_start": base.splits.test_end}),
        }
    )


def _full_features(settings: Settings) -> pd.DataFrame:
    build_tabular_features(settings)
    build_graph_features(settings)
    build_motif_features(settings)
    tabular, graph, motifs = (
        sql_literal(str(settings.features_dir / f"{n}.parquet"))
        for n in ("tabular", "graph", "motifs")
    )
    return duckdb.sql(
        f"""
        SELECT t.*, g.* EXCLUDE (transaction_id), m.* EXCLUDE (transaction_id)
        FROM read_parquet({tabular}) AS t
        JOIN read_parquet({graph}) AS g USING (transaction_id)
        JOIN read_parquet({motifs}) AS m USING (transaction_id)
        ORDER BY transaction_id
        """
    ).df()


def _batch_features(settings: Settings, days: list[date]) -> pd.DataFrame:
    files = [sql_literal(str(day_dir(settings, d) / "features.parquet")) for d in days]
    return duckdb.sql(
        f"SELECT * FROM read_parquet([{', '.join(files)}]) ORDER BY transaction_id"
    ).df()


def test_a_day_at_a_time_gives_the_features_and_alerts_of_a_single_pass(
    dense_settings: Settings,
) -> None:
    batch = Batch(dense_settings)
    assert batch.days == [date(2022, 9, d) for d in range(5, 11)]
    batch.replay()

    full = _full_features(dense_settings)
    by_day = _batch_features(dense_settings, batch.days)
    assert full["relay_depth"].max() >= 3  # chains long enough to cross days
    pd.testing.assert_frame_equal(by_day, full, check_exact=True)

    evaluate_rules(dense_settings)
    single_pass = {
        (a.alert_id, a.triggered_at, a.value, a.evidence)
        for part in sorted(dense_settings.rule_alerts_dir.glob("part-*.parquet"))
        for a in read_alerts(part)
    }
    day_by_day = {
        (a.alert_id, a.triggered_at, a.value, a.evidence)
        for d in batch.days
        for a in read_alerts(day_dir(dense_settings, d) / "rule_alerts.parquet")
    }
    assert {a[0].split(":")[0] for a in single_pass} >= {"R01", "R02", "R04"}
    assert day_by_day == single_pass


def test_a_day_waits_for_the_days_before_it(dense_settings: Settings) -> None:
    batch = Batch(dense_settings)
    with pytest.raises(MissingDaysError, match="2022-09-05"):
        batch.run_day(date(2022, 9, 6))
    batch.replay(last=date(2022, 9, 6))

    # A day left incomplete blocks the next one until it runs again.
    (day_dir(dense_settings, date(2022, 9, 6)) / "manifest.json").unlink()
    with pytest.raises(MissingDaysError, match="2022-09-06"):
        batch.run_day(date(2022, 9, 7))


def test_running_a_day_again_replaces_it(dense_settings: Settings) -> None:
    batch = Batch(dense_settings)
    batch.replay(last=date(2022, 9, 6))
    day = date(2022, 9, 6)
    first = load_manifest(dense_settings, day)
    before = _batch_features(dense_settings, [day])
    second = batch.run_day(day)
    assert first is not None and second.written_at > first.written_at
    assert second.model_dump(exclude={"written_at"}) == first.model_dump(exclude={"written_at"})
    pd.testing.assert_frame_equal(_batch_features(dense_settings, [day]), before)
    assert not list(day_dir(dense_settings, day).glob(".*.tmp"))


def test_days_after_a_rule_change_must_be_replayed(dense_settings: Settings) -> None:
    batch = Batch(dense_settings)
    batch.replay(last=date(2022, 9, 6))
    changed = batch.rules[0].model_copy(update={"version": batch.rules[0].version + ".1"})
    batch.rules = [changed, *batch.rules[1:]]
    with pytest.raises(MissingDaysError, match="other rule versions"):
        batch.run_day(date(2022, 9, 7))


@pytest.fixture
def replayed(fitted_settings: Settings, tmp_path: Path) -> Settings:
    """The fitted model fixture (warm-up 5 Sep, train 6, validation 7, test 8 Sep) replayed by
    the batch, with the single-pass rule alerts beside it."""
    settings = fitted_settings.model_copy(
        update={
            "data_dir": tmp_path,
            "batch": fitted_settings.batch.model_copy(
                update={"dir": tmp_path / "batch", "serving_path": tmp_path / "serving.duckdb"}
            ),
        }
    )
    evaluate_rules(settings)  # the fixture's rule alerts are hand-made
    Batch(settings).replay()
    return settings


def test_days_are_scored_queued_and_checked_for_drift_by_phase(replayed: Settings) -> None:
    phases = {d: load_manifest(replayed, date(2022, 9, d)) for d in (5, 6, 7, 8)}
    assert {d: m.phase for d, m in phases.items() if m} == {
        5: "warm-up",
        6: "train",
        7: "validation",
        8: "test",
    }
    version = Registry(replayed).champion_version()
    assert phases[5].champion is None and phases[5].alerts is None
    assert all(phases[d].champion.version == version for d in (6, 7, 8))
    assert [phases[d].drift_detected is None for d in (5, 6, 7, 8)] == [True, True, False, False]


def test_the_queue_is_the_one_the_evaluation_builds(replayed: Settings) -> None:
    evaluator = SplitEvaluator(replayed, "validation")
    data = load_split(replayed, "validation")
    champion = Registry(replayed).load_champion()
    evaluator.set_scores(data.transaction_ids, transaction_scores(champion, data.features))
    evaluator.evaluate_rules(replayed.rule_alerts_dir, load_rules(replayed.rules_dir))
    expected = queue_cases(
        evaluator.ranking(top=replayed.agent_evals.top_transactions),
        evaluator.rule_hits(),
        replayed.batch.alert_budget,
    )
    queue = duckdb.read_parquet(str(day_dir(replayed, date(2022, 9, 7)) / "alerts.parquet")).df()
    assert len(queue) > 0
    assert list(queue["alert_id"]) == list(expected["alert_id"])
    assert [tuple(s) for s in queue["sources"]] == list(expected["sources"])
    assert [tuple(t) for t in queue["transaction_ids"]] == list(expected["transaction_ids"])
    np.testing.assert_allclose(queue["score"], expected["score"])
    assert list(queue["rank"]) == list(expected["rank"])


def test_drift_matches_the_split_report(replayed: Settings) -> None:
    (expected,) = detect_drift(replayed, "validation").days
    path = day_dir(replayed, date(2022, 9, 7)) / "drift.json"
    from atalayero.monitoring.drift import DayDrift

    drift = DayDrift.model_validate_json(path.read_text())
    assert drift.rule_alerts == expected.rule_alerts
    assert drift.rule_alerts_reference == pytest.approx(expected.rule_alerts_reference)
    assert {f.feature: f.psi for f in drift.features} == pytest.approx(
        {f.feature: f.psi for f in expected.features}
    )


def test_the_serving_database_holds_the_complete_days_and_no_labels(replayed: Settings) -> None:
    (day_dir(replayed, date(2022, 9, 8)) / "manifest.json").unlink()  # 8 Sep left incomplete
    reader = duckdb.connect()
    path = publish(replayed)
    reader.execute(f"ATTACH {sql_literal(str(path))} AS old (READ_ONLY)")
    batch_alerts = duckdb.read_parquet(
        str(day_dir(replayed, date(2022, 9, 6)) / "alerts.parquet")
    ).df()

    # A reader holding the published file does not stop the next publication.
    publish(replayed)
    with duckdb.connect(str(path), read_only=True) as con:
        days = con.execute("SELECT day, phase FROM days ORDER BY day").fetchall()
        assert days == [
            (date(2022, 9, 5), "warm-up"),
            (date(2022, 9, 6), "train"),
            (date(2022, 9, 7), "validation"),
        ]
        alerted = con.execute("SELECT DISTINCT day FROM alerts ORDER BY day").fetchall()
        assert alerted == [(date(2022, 9, 6),), (date(2022, 9, 7),)]
        (n,) = con.execute("SELECT count(*) FROM alerts WHERE day = DATE '2022-09-06'").fetchone()
        assert n == len(batch_alerts)
        # Every transaction of an alerted account-day touches its account, on its day.
        (stray,) = con.execute(
            """
            SELECT count(*) FROM alert_transactions AS t JOIN alerts AS a USING (alert_id)
            WHERE t.transacted_at::date <> a.day
                OR a.account_key NOT IN (t.sender_account_key, t.receiver_account_key)
            """
        ).fetchone()
        assert stray == 0
        (raised,) = con.execute(
            "SELECT count(*) FROM alert_transactions WHERE raised_alert"
        ).fetchone()
        assert raised > 0
        (drift,) = con.execute("SELECT count(*) FROM drift").fetchone()
        assert drift == 1
        columns = {
            c.lower()
            for (c,) in con.execute("SELECT column_name FROM information_schema.columns").fetchall()
        }
    assert not {c for c in columns if any(w in c for w in ("laundering", "label", "typolog"))}
    reader.close()


def test_serving_tables_exist_before_any_day_is_queued(dense_settings: Settings) -> None:
    Batch(dense_settings).run_day(date(2022, 9, 5))
    with duckdb.connect(str(publish(dense_settings)), read_only=True) as con:
        counts = {
            t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("days", "alerts", "rule_alerts", "alert_transactions", "drift")
        }
    assert counts["days"] == 1 and counts["alerts"] == 0 and counts["drift"] == 0


def test_a_day_outside_the_simulation_is_refused(dense_settings: Settings) -> None:
    with pytest.raises(ValueError, match="outside the simulation"):
        Batch(dense_settings).run_day(date(2022, 9, 11))
    assert datetime(2022, 9, 11) == dense_settings.splits.test_end
