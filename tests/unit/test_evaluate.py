import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from atalayero.models.evaluate import Coverage, SplitEvaluator, evaluate_rules_only
from atalayero.rules.schema import load_rules
from atalayero.schemas import Alert, Transaction
from atalayero.settings import Settings
from atalayero.streaming.consumer import AlertSink

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]

REPO_ROOT = Path(__file__).parents[2]
T0 = datetime(2022, 9, 5, 9)  # as in conftest
DAY = 24 * 60  # minutes
DAY_1 = date(2022, 9, 5)

# transaction_id -> (is_laundering, label_group, attempt_id, typology)
LABELS = {
    0: (False, "clean", None, None),  # train: makes 001:0 the hub, which sorts first on ties
    1: (True, "patterned", 1, "fan_out"),  # A -> B
    2: (False, "clean", None, None),  # C -> D
    3: (True, "untyped", None, None),  # E -> F
    4: (True, "patterned", 2, "cycle"),  # 0 -> G, from the hub
    5: (False, "clean", None, None),  # A -> C, day 2
    6: (True, "patterned", 1, "fan_out"),  # A -> D, day 2
}


@pytest.fixture
def settings(
    make_tx: MakeTx, fct_transactions_db: WriteDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    """Validation is 5-6 Sep; the train split before it holds only the hub's history."""
    monkeypatch.chdir(REPO_ROOT)
    transactions = [
        make_tx(0, -DAY, sender="001:0", receiver="001:Z"),
        make_tx(1, 0, sender="001:A", receiver="001:B"),
        make_tx(2, 10, sender="001:C", receiver="001:D"),
        make_tx(3, 20, sender="001:E", receiver="001:F"),
        make_tx(4, 30, sender="001:0", receiver="001:G"),
        make_tx(5, DAY, sender="001:A", receiver="001:C"),
        make_tx(6, DAY + 10, sender="001:A", receiver="001:D"),
    ]
    db = fct_transactions_db(transactions)
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE marts.fct_laundering_labels (transaction_id BIGINT, "
            "is_laundering BOOLEAN, label_group VARCHAR, attempt_id INTEGER, typology VARCHAR)"
        )
        con.executemany(
            "INSERT INTO marts.fct_laundering_labels VALUES (?, ?, ?, ?, ?)",
            [(i, *label) for i, label in LABELS.items()],
        )
    base = Settings()
    return base.model_copy(
        update={
            "duckdb_path": db,
            "rule_alerts_dir": tmp_path / "alerts",
            "splits": base.splits.model_copy(
                update={
                    "train_end": datetime(2022, 9, 5),
                    "validation_end": datetime(2022, 9, 7),
                    "test_end": datetime(2022, 9, 9),
                }
            ),
            "evaluation": base.evaluation.model_copy(
                update={"hub_accounts": 1, "reports_dir": tmp_path / "reports"}
            ),
        }
    )


RULES = load_rules(REPO_ROOT / "config" / "rules")
VERSIONS = {rule.id: rule.version for rule in RULES}
ALL_RULES = "rules " + ", ".join(f"{r.id} v{r.version}" for r in RULES)


@pytest.fixture
def write_alerts() -> Callable[[Path, list[tuple[str, str, int]]], None]:
    """Write (rule_id, account_key, minutes after T0) as alerts of the current rule versions."""

    def write(directory: Path, alerts: list[tuple[str, str, int]]) -> None:
        sink = AlertSink(directory)
        sink.extend(
            Alert(
                alert_id=f"{rule}:{i}",
                rule_id=rule,
                rule_version=VERSIONS.get(rule, "0.9"),
                account_key=account,
                triggered_at=T0 + timedelta(minutes=minutes),
                transaction_id=i,
                value=1,
                evidence=(i,),
            )
            for i, (rule, account, minutes) in enumerate(alerts)
        )
        sink.flush()

    return write


WriteAlerts = Callable[[Path, list[tuple[str, str, int]]], None]


def test_rule_alerts_cover_both_accounts_of_a_transaction(
    settings: Settings, write_alerts: WriteAlerts
) -> None:
    write_alerts(
        settings.rule_alerts_dir,
        [
            ("R01", "001:B", 5),  # receiver of 1
            ("R02", "001:C", 15),  # nothing laundered on day 1: false positive
            ("R02", "001:0", 40),  # the hub, sender of 4
            ("R01", "001:B", 50),  # same account-day again: one alert
            ("R04", "001:E", -DAY),  # before the split: ignored
        ],
    )

    m = SplitEvaluator(settings, "validation").evaluate_rules(settings.rule_alerts_dir, RULES)

    assert m.detector == ALL_RULES
    assert (m.days, m.budget, m.alerts, m.alerts_per_day) == (2, None, 3, 1.5)
    assert (m.false_positive_share, m.hub_alert_share) == (pytest.approx(1 / 3),) * 2
    assert m.laundering == Coverage(covered=2, total=4)
    assert (m.patterned, m.untyped) == (Coverage(covered=2, total=3), Coverage(covered=0, total=1))
    assert m.attempts == Coverage(covered=2, total=2)
    assert m.typologies == {
        "cycle": Coverage(covered=1, total=1),
        "fan_out": Coverage(covered=1, total=2),
    }
    assert m.laundering_without_hubs == Coverage(covered=1, total=4)


def test_an_alert_covers_only_its_own_day(settings: Settings, write_alerts: WriteAlerts) -> None:
    write_alerts(settings.rule_alerts_dir, [("R02", "001:A", DAY + 20)])  # 6, not 1

    m = SplitEvaluator(settings, "validation").evaluate_rules(settings.rule_alerts_dir, RULES)

    assert m.laundering == Coverage(covered=1, total=4)
    assert m.attempts == Coverage(covered=1, total=2)
    assert m.false_positive_share == 0


def test_rules_can_be_evaluated_one_by_one(settings: Settings, write_alerts: WriteAlerts) -> None:
    write_alerts(settings.rule_alerts_dir, [("R01", "001:B", 5), ("R02", "001:C", 15)])
    evaluator = SplitEvaluator(settings, "validation")

    [r02] = [rule for rule in RULES if rule.id == "R02"]
    m = evaluator.evaluate_rules(settings.rule_alerts_dir, [r02])

    assert (m.detector, m.alerts, m.laundering.covered) == (f"rules R02 v{r02.version}", 1, 0)
    assert evaluator.rule_budget() == {DAY_1: 1}


def test_scores_rank_account_days_by_their_best_transaction(settings: Settings) -> None:
    evaluator = SplitEvaluator(settings, "validation")
    # Day 1: E, F 0.9 (via 3), tied, E first; the hub and G 0.5 (via 4), the hub first;
    # C, D 0.2; A, B 0.1.
    # Day 2: A, D 0.8 (via 6); C 0.3.
    ids = np.array([0, 1, 2, 3, 4, 5, 6])
    evaluator.set_scores(ids, np.array([0.99, 0.1, 0.2, 0.9, 0.5, 0.3, 0.8]))

    top1 = evaluator.evaluate_scores("model", 1)
    top3 = evaluator.evaluate_scores("model", 3)

    assert (top1.budget, top1.alerts, top1.laundering.covered) == (1, 2, 2)  # E: 3; A: 6
    assert top1.false_positive_share == 0
    # + F and the hub (4) on day 1; D and C on day 2, where C has nothing laundered
    assert (top3.alerts, top3.laundering.covered) == (6, 3)
    assert top3.false_positive_share == pytest.approx(1 / 6)
    assert top3.laundering_without_hubs.covered == 2  # 4 only through the hub


def test_budget_can_follow_the_rules_day_by_day(settings: Settings) -> None:
    evaluator = SplitEvaluator(settings, "validation")
    evaluator.set_scores(np.arange(7), np.array([0, 0.1, 0.2, 0.9, 0.5, 0.3, 0.8]))

    m = evaluator.evaluate_scores("model", {DAY_1: 2})  # day 2 is missing: no alerts

    assert (m.budget, m.alerts, m.laundering.covered) == (None, 2, 1)


def test_every_transaction_of_the_split_needs_a_score(settings: Settings) -> None:
    evaluator = SplitEvaluator(settings, "validation")

    with pytest.raises(ValueError, match="2 transactions of the validation split have no score"):
        evaluator.set_scores(np.array([0, 1, 2, 3, 4]), np.zeros(5))


def test_curve_and_pr_auc(settings: Settings) -> None:
    evaluator = SplitEvaluator(settings, "validation")
    evaluator.set_scores(np.arange(7), np.array([0, 0.1, 0.2, 0.9, 0.5, 0.3, 0.8]))

    curve = evaluator.curve([3, 1, 100])

    assert [p.budget for p in curve] == [1, 3, 100]
    assert [p.detection for p in curve] == [0.5, 0.75, 1.0]
    assert [p.detection_without_hubs for p in curve] == [0.5, 0.5, 1.0]  # at 3, 4 via the hub
    assert [p.alerts_per_day for p in curve] == [1.0, 3.0, 5.5]  # day 2 has only 3 accounts
    assert curve[0].false_positive_share == 0
    # by score: 3, 6, 4 laundering, then 5 and 2 clean, then 1 laundering
    assert evaluator.pr_auc() == pytest.approx((1 + 1 + 1 + 4 / 6) / 4)


def test_rules_only_report(settings: Settings, write_alerts: WriteAlerts) -> None:
    write_alerts(settings.rule_alerts_dir, [("R01", "001:B", 5)])

    path = evaluate_rules_only(settings, "validation")

    report = json.loads(path.read_text())
    assert path.name == "rules_only_validation.json"
    assert [r["detector"] for r in report["results"]] == [
        ALL_RULES,
        *(f"rules {r.id} v{r.version}" for r in RULES),
    ]
    assert report["results"][0]["laundering"] == {"covered": 1, "total": 4, "rate": 0.25}
    assert report["results"][2]["alerts"] == 0  # R02 has no alerts here


def test_rules_need_their_alerts(settings: Settings) -> None:
    with pytest.raises(FileNotFoundError, match="run `make rules` first"):
        SplitEvaluator(settings, "validation").evaluate_rules(settings.rule_alerts_dir, RULES)


def test_alerts_of_another_rule_version_are_rejected(
    settings: Settings, write_alerts: WriteAlerts
) -> None:
    write_alerts(settings.rule_alerts_dir, [("R01", "001:B", 5)])
    old = [r.model_copy(update={"version": "0.9"}) if r.id == "R01" else r for r in RULES]

    with pytest.raises(ValueError, match="alerts from R01 v1.0 differ from config/rules"):
        SplitEvaluator(settings, "validation").evaluate_rules(settings.rule_alerts_dir, old)


def test_detections_and_alerts_describe_the_last_evaluation(
    settings: Settings, write_alerts: WriteAlerts
) -> None:
    write_alerts(settings.rule_alerts_dir, [("R01", "001:B", 5), ("R02", "001:0", 40)])
    evaluator = SplitEvaluator(settings, "validation")
    m = evaluator.evaluate_rules(settings.rule_alerts_dir, RULES)

    detections = evaluator.detections()
    alerts = evaluator.alerts()

    assert detections["transaction_id"].tolist() == [1, 3, 4, 6]  # the laundering ones
    assert detections["detected"].tolist() == [True, False, True, False]
    assert detections["via_other_than_hub"].tolist() == [True, False, False, False]
    assert detections["label_group"].tolist() == ["patterned", "untyped", "patterned", "patterned"]
    assert (detections["payment_format"] == "ach").all()
    assert detections["detected"].sum() == m.laundering.covered
    assert alerts["account_key"].tolist() == ["001:0", "001:B"]
    assert alerts["on_hub"].tolist() == [True, False]
    assert alerts["has_laundering"].tolist() == [True, True]
    assert alerts["transactions"].tolist() == [1, 1]  # the hub's history is on the day before
    assert len(alerts) == m.alerts
