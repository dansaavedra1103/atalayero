import asyncio
import math
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from mcp import Client

from atalayero.agent.tools import MAX_NEIGHBOURS, MAX_TRANSACTIONS, TOP_FACTORS, ToolBox
from atalayero.mcp_server.server import build_server
from atalayero.models.data import FEATURES, load_split
from atalayero.models.families import transaction_scores
from atalayero.models.holdout import run_holdout
from atalayero.models.registry import Registry
from atalayero.models.train import train_and_evaluate
from atalayero.schemas import CaseAlert, Transaction
from atalayero.settings import Settings

MakeTx = Callable[..., Transaction]
WriteDb = Callable[[list[Transaction]], Path]

SRC = Path(__file__).parents[2] / "src" / "atalayero"
DAY = date(2022, 9, 5)  # T0 is 2022-09-05 09:00
CUTOFF = datetime(2022, 9, 6)
HOUR = 60


def _alert(account: str, day: date = DAY) -> CaseAlert:
    return CaseAlert(
        alert_id=f"{day.isoformat()}:{account}",
        account_key=account,
        day=day,
        sources=("model",),
        score=0.5,
        rank=1,
        transaction_ids=(1,),
    )


A, H = _alert("001:A"), _alert("001:H")


@pytest.fixture
def toolbox(make_tx: MakeTx, fct_transactions_db: WriteDb, tmp_path: Path) -> ToolBox:
    """A's story on 5 Sep: it sends to B, B sends back (A→B→A) and on to C, C pays A (A→B→C→A).
    Before: A paid B on 4 Sep, and C paid A just before A paid B. After the cut-off: D pays A at
    midnight and A pays E the next day. H sends 80 transfers on 5 Sep."""
    transactions = [
        make_tx(0, -24 * HOUR, sender="001:A", receiver="001:B", usd=1000.0),
        make_tx(1, 0, sender="001:A", receiver="001:B"),
        make_tx(2, HOUR, sender="001:B", receiver="001:C", usd=5800.0),
        make_tx(3, 2 * HOUR, sender="001:C", receiver="001:A", usd=5600.0),
        make_tx(4, 30, sender="001:B", receiver="001:A", usd=300.0),
        make_tx(5, 15 * HOUR, sender="001:D", receiver="001:A"),  # 6 Sep 00:00, the cut-off
        make_tx(6, 24 * HOUR, sender="001:A", receiver="001:E"),
        make_tx(7, -10, sender="001:C", receiver="001:A", usd=700.0),
        *(
            make_tx(100 + i, i, sender="001:H", receiver=f"002:{i}", usd=100.0 + i)
            for i in range(80)
        ),
    ]
    db = fct_transactions_db(transactions)
    return ToolBox(Settings().model_copy(update={"duckdb_path": db}), [A, H])


def _outputs(toolbox: ToolBox) -> dict[str, Any]:
    return {
        "profile": toolbox.account_profile(A.alert_id),
        "transactions": toolbox.transactions(A.alert_id, days=7),
        "neighbourhood": toolbox.neighbourhood(A.alert_id, hops=2, days=7),
        "counterparty": toolbox.account_profile(A.alert_id, "001:C"),
    }


def test_tools_see_nothing_from_after_the_alerts_day(toolbox: ToolBox) -> None:
    profile = toolbox.account_profile(A.alert_id)
    rows = toolbox.transactions(A.alert_id, days=7)["transactions"]

    assert profile["visible_until"] == CUTOFF.isoformat()
    assert profile["alert_day"]["sent"] == {"count": 1, "usd": 6000.0, "counterparties": 1}
    assert profile["alert_day"]["received"] == {"count": 3, "usd": 6600.0, "counterparties": 2}
    assert profile["history"]["sent"]["count"] == 2  # 4 Sep and 5 Sep; not 6 Sep
    # newest first; 2 is between B and C; 5 and 6 are after the cut-off
    assert [r["transaction_id"] for r in rows] == [3, 4, 1, 7, 0]
    assert rows[0]["direction"] == "in" and rows[2]["direction"] == "out"


def test_removing_everything_after_the_cut_off_changes_nothing(toolbox: ToolBox) -> None:
    before = _outputs(toolbox)
    toolbox.con.close()
    db = toolbox.settings.duckdb_path
    with duckdb.connect(str(db)) as con:
        con.execute("DELETE FROM marts.fct_transactions WHERE transacted_at >= ?", [CUTOFF])

    after = _outputs(ToolBox(toolbox.settings, [A, H]))

    assert after == before


def test_neighbourhood_lists_cycles_in_time_order(toolbox: ToolBox) -> None:
    result = toolbox.neighbourhood(A.alert_id, hops=2, days=1)

    assert result["cycles"] == [
        {"path": ["001:A", "001:B", "001:A"], "transaction_ids": [1, 4], "usd": [6000.0, 300.0]},
        {
            "path": ["001:A", "001:B", "001:C", "001:A"],
            "transaction_ids": [1, 2, 3],  # 7 pays A before A pays B: no cycle
            "usd": [6000.0, 5800.0, 5600.0],
        },
    ]
    top = {c["account_key"]: c for c in result["top_counterparties"]}
    assert top["001:B"]["sent_to"] == {"count": 1, "usd": 6000.0}
    assert top["001:B"]["received_from"] == {"count": 1, "usd": 300.0}
    assert {s["account_key"] for s in result["second_hop"]} == {"001:B", "001:C"}  # via 2


def test_outputs_are_capped_for_busy_accounts(toolbox: ToolBox) -> None:
    rows = toolbox.transactions(H.alert_id, limit=500)
    graph = toolbox.neighbourhood(H.alert_id)

    assert (rows["shown"], rows["sent"]["count"]) == (MAX_TRANSACTIONS, 80)
    assert rows["sent"]["usd"] == pytest.approx(sum(100.0 + i for i in range(80)))
    assert (len(graph["top_counterparties"]), graph["counterparties"]) == (MAX_NEIGHBOURS, 80)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_no_tool_reads_or_returns_a_label(toolbox: ToolBox) -> None:
    keys = {k for output in _outputs(toolbox).values() for k in _keys(output)}

    assert not [k for k in keys if any(w in k for w in ("launder", "label", "typolog", "attempt"))]
    sources = [*(SRC / "agent").rglob("*.py"), *(SRC / "mcp_server").rglob("*.py")]
    for path in sources:
        text = path.read_text()
        for forbidden in (
            "fct_laundering_labels",
            "laundering_attempts",
            "is_laundering",
            "load_answers",
            "_answers.jsonl",
        ):
            assert forbidden not in text, f"{path.name} mentions {forbidden}"


def test_an_unknown_alert_is_refused(toolbox: ToolBox) -> None:
    with pytest.raises(ValueError, match="unknown alert"):
        toolbox.transactions("2022-09-09:001:A")


def test_the_mcp_server_serves_the_tools(toolbox: ToolBox) -> None:
    server = build_server(toolbox)

    async def session() -> tuple[set[str], Any, Any]:
        async with Client(server) as client:
            tools = await client.list_tools()
            ok = await client.call_tool("get_transactions", {"alert_id": A.alert_id, "days": 7})
            bad = await client.call_tool("get_account_profile", {"alert_id": "nope"})
            return {t.name for t in tools.tools}, ok, bad

    names, ok, bad = asyncio.run(session())

    assert names == {
        "get_account_profile",
        "get_transactions",
        "get_graph_neighborhood",
        "explain_score",
        "search_typologies",
    }
    assert ok.structured_content == toolbox.transactions(A.alert_id, days=7)
    assert bad.is_error and "unknown alert 'nope'" in bad.content[0].text


@pytest.fixture
def lightgbm_settings(
    holdout_settings: Settings, small_params: dict[str, dict], tmp_path: Path
) -> Settings:
    """The holdout fixture with LightGBM alone, so it is the champion, fitted for both splits."""
    config = tmp_path / "lightgbm.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "families": {"lightgbm": {"params": small_params["lightgbm"]}},
                "history": [{"version": "1.0", "date": "2026-10-06", "reason": "Tests"}],
            }
        )
    )
    settings = holdout_settings.model_copy(
        update={"models": holdout_settings.models.model_copy(update={"config_path": config})}
    )
    train_and_evaluate(settings)
    run_holdout(settings)
    return settings


def test_explain_score_uses_the_model_behind_each_split(lightgbm_settings: Settings) -> None:
    settings = lightgbm_settings
    test_day, validation_day = date(2022, 9, 8), date(2022, 9, 7)
    with duckdb.connect(str(settings.duckdb_path), read_only=True) as con:
        (account,) = con.execute(
            "SELECT sender_account_key FROM marts.fct_transactions WHERE transacted_at::date = ? "
            "AND sender_account_key IN (SELECT sender_account_key FROM marts.fct_transactions "
            "WHERE transacted_at::date = ?) GROUP BY ALL ORDER BY count(*) DESC, 1 LIMIT 1",
            [test_day, validation_day],
        ).fetchone()
    alerts = [_alert(account, test_day), _alert(account, validation_day)]
    toolbox = ToolBox(settings, alerts)
    holdout = (
        duckdb.read_parquet(str(settings.evaluation.reports_dir / "holdout_scores.parquet"))
        .df()
        .set_index("transaction_id")["lightgbm"]
    )
    validation = load_split(settings, "validation")
    champion = dict(
        zip(
            validation.transaction_ids,
            transaction_scores(Registry(settings).load_champion(), validation.features),
            strict=True,
        )
    )

    on_test, on_validation = (toolbox.explain_score(a.alert_id) for a in alerts)

    for result, scores in ((on_test, holdout), (on_validation, champion)):
        top = result["explained"][0]
        assert top["score"] == pytest.approx(scores[top["transaction_id"]], abs=1e-6)
        assert 0 < len(top["top_factors"]) <= TOP_FACTORS
        assert {f["feature"] for f in top["top_factors"]} <= set(FEATURES)
        magnitudes = [abs(f["log_odds"]) for f in top["top_factors"]]
        assert magnitudes == sorted(magnitudes, reverse=True)
        assert math.isfinite(top["baseline_log_odds"])
