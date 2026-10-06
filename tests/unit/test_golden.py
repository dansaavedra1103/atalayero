import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from atalayero.evals.golden import (
    CaseAnswer,
    build_case_set,
    label_account_days,
    load_alerts,
    load_answers,
    queue_cases,
    sample_cases,
)
from atalayero.schemas import CaseAlert
from atalayero.settings import Settings

DAY = date(2022, 9, 9)


def test_account_days_take_the_group_of_their_laundering() -> None:
    detections = pd.DataFrame(
        {
            "transaction_id": [1, 2, 3, 4],
            "transacted_at": [datetime(2022, 9, 9, h) for h in (1, 2, 3, 4)],
            "sender_account_key": ["A", "A", "C", "E"],
            "receiver_account_key": ["B", "B", "A", "E"],  # 4 is a self-transfer
            "label_group": ["patterned", "patterned", "untyped", "untyped"],
            "typology": ["stack", "cycle", None, None],
        }
    )

    labels = label_account_days(detections).set_index("account_key")

    # A: two typologies tied, plus untyped laundering, is patterned; C and E only untyped
    assert labels.loc["A", "group"] == "patterned"
    assert labels.loc["A", "typologies"] == ("cycle", "stack")
    assert labels.loc["A", "main_typology"] == "cycle"
    assert labels.loc["B", "typologies"] == ("cycle", "stack")
    assert labels.loc["C", "group"] == labels.loc["E", "group"] == "untyped"
    assert labels.loc["C", "typologies"] == ()


def test_the_queue_joins_rules_and_model_and_leaves_hubs_out() -> None:
    ranking = pd.DataFrame(
        {
            "day": [DAY] * 4,
            "account_key": ["H", "A", "B", "C"],
            "score": [0.9, 0.8, 0.5, 0.1],
            "rank": [1, 2, 3, 4],
            "on_hub": [True, False, False, False],
            "top_transactions": [[9], [1, 2], [3], [4]],
        }
    )
    hits = pd.DataFrame(
        {
            "rule_id": ["R02", "R01", "R04"],
            "day": [DAY] * 3,
            "account_key": ["H", "C", "C"],
            "transaction_id": [9, 7, 4],
        }
    )

    queue = queue_cases(ranking, hits, budget=2).set_index("account_key")

    assert list(queue.index) == ["A", "C"]  # H is a hub; B is outside the budget, with no rule
    assert queue.loc["A", "sources"] == ("model",)
    assert queue.loc["C", "sources"] == ("R01", "R04")
    assert queue.loc["C", "transaction_ids"] == (4, 7)  # top-scored and triggering, once each
    assert queue.loc["A", "alert_id"] == "2022-09-09:A"


def _candidates() -> pd.DataFrame:
    rows = []
    for i in range(6):
        rows.append(("stack" if i < 5 else "cycle", "patterned", ("model",)))
    rows += [(None, "untyped", ("model",))] * 3
    rows += [(None, "clean", ("R04",))] * 4 + [(None, "clean", ("model",))] * 1
    return pd.DataFrame(
        {
            "alert_id": [f"{i:02d}" for i in range(len(rows))],
            "main_typology": [r[0] for r in rows],
            "group": [r[1] for r in rows],
            "sources": [r[2] for r in rows],
        }
    )


def test_sampling_spreads_typologies_and_sources() -> None:
    selected = sample_cases(_candidates(), per_group=4, seed=0)

    by_group = selected.groupby("group")
    assert by_group.size().to_dict() == {"clean": 4, "patterned": 4, "untyped": 3}
    patterned = selected[selected["group"] == "patterned"]
    assert patterned["main_typology"].value_counts().to_dict() == {"stack": 3, "cycle": 1}
    clean = selected[selected["group"] == "clean"]
    assert clean["sources"].value_counts().to_dict() == {("R04",): 3, ("model",): 1}  # filled


def test_sampling_is_deterministic_and_shuffles_the_groups() -> None:
    first = sample_cases(_candidates(), per_group=3, seed=0)
    again = sample_cases(_candidates(), per_group=3, seed=0)
    other = sample_cases(_candidates(), per_group=3, seed=1)

    assert first["alert_id"].tolist() == again["alert_id"].tolist()
    assert first["alert_id"].tolist() != other["alert_id"].tolist()
    assert first["group"].tolist() != sorted(first["group"].tolist())


@pytest.fixture
def case_settings(fitted_settings: Settings, tmp_path: Path) -> Settings:
    """Case sets from the model fixture: a champion for validation, holdout scores for test."""
    return fitted_settings.model_copy(
        update={
            "agent_evals": fitted_settings.agent_evals.model_copy(
                update={
                    "queue_budget": 5,
                    "dev_cases": 2,
                    "golden_cases": 3,
                    "dir": tmp_path / "evals",
                }
            )
        }
    )


def test_case_sets_keep_answers_apart_and_rebuild_identically(case_settings: Settings) -> None:
    settings = case_settings

    paths = [build_case_set(settings, case_set) for case_set in ("dev", "golden")]
    first = [p.read_bytes() for pair in paths for p in pair]
    build_case_set(settings, "dev")
    build_case_set(settings, "golden")

    assert [p.read_bytes() for pair in paths for p in pair] == first
    for case_set in ("dev", "golden"):
        alerts, answers = load_alerts(settings, case_set), load_answers(settings, case_set)
        assert [a.alert_id for a in alerts]
        assert {a.alert_id for a in alerts} == set(answers)
        for line in (
            (settings.agent_evals.dir / f"{case_set}_alerts.jsonl").read_text().splitlines()
        ):
            assert set(json.loads(line)) == set(CaseAlert.model_fields)  # nothing else
        assert all(isinstance(a, CaseAnswer) for a in answers.values())
    golden = load_alerts(settings, "golden")
    assert {a.day for a in golden} == {date(2022, 9, 8)}  # the test day of the fixture
    with duckdb.connect(str(settings.duckdb_path), read_only=True) as con:
        (hub,) = con.execute(  # the fixture's top sender in train (hub_accounts: 1)
            "SELECT sender_account_key FROM marts.fct_transactions WHERE transacted_at < ? "
            "GROUP BY ALL ORDER BY count(*) DESC, sender_account_key LIMIT 1",
            [settings.splits.train_end],
        ).fetchone()
    assert all(
        a.account_key != hub
        for case_set in ("dev", "golden")
        for a in load_alerts(settings, case_set)
    )
