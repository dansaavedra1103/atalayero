import asyncio
import shutil
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from atalayero.agent.graph import Investigation
from atalayero.agent.grounding import Grounding
from atalayero.agent.run import investigate_day
from atalayero.api.app import create_app
from atalayero.api.security import SECURITY_HEADERS, TokenBucket, key_digest
from atalayero.batch import queries
from atalayero.batch.day import day_dir
from atalayero.batch.serving import publish
from atalayero.schemas import CaseAlert, CaseReport, Evidence
from atalayero.settings import Settings

KEY = "test-key-0123456789"


def _settings(base: Settings, **api: object) -> Settings:
    return base.model_copy(
        update={
            "api": base.api.model_copy(
                update={
                    "key_hashes": (key_digest(KEY),),
                    "allowed_hosts": ("testserver",),
                    **api,
                }
            )
        }
    )


@pytest.fixture(scope="module")
def served(replayed_once: Settings) -> Settings:
    """The replayed fixture, published once for every test here: they only read it."""
    publish(replayed_once)
    return _settings(replayed_once)


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture
def client(served: Settings) -> TestClient:
    return _client(served)


def _get(client: TestClient, path: str, **params: object) -> object:
    return client.get(path, params=params, headers={"X-API-Key": KEY})


def test_health_needs_no_key_and_says_little(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "data": "published"}


def test_every_response_carries_the_security_headers(client: TestClient) -> None:
    for response in (client.get("/health"), client.get("/alerts"), client.post("/alerts")):
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
        assert response.headers["x-request-id"]
        assert "server" not in response.headers


def test_a_request_id_from_the_gateway_comes_back(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


def test_data_needs_a_valid_key(client: TestClient) -> None:
    assert client.get("/alerts").status_code == 401
    wrong = client.get("/alerts", headers={"X-API-Key": "nope"})
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"] == "ApiKey"
    assert _get(client, "/alerts").status_code == 200


def test_a_client_that_keeps_failing_is_slowed_down(client: TestClient) -> None:
    codes = [client.get("/alerts", headers={"X-API-Key": "guess"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[-1] == 429
    response = client.get("/alerts", headers={"X-API-Key": "guess"})
    assert int(response.headers["retry-after"]) >= 1


def test_each_key_has_a_rate(served: Settings) -> None:
    client = _client(_settings(served, requests_per_minute=1, burst=2))
    codes = [_get(client, "/alerts").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_the_api_refuses_to_start_without_keys(served: Settings) -> None:
    with pytest.raises(ValueError, match="no API key"):
        create_app(_settings(served, key_hashes=()))


def test_the_api_only_reads(client: TestClient) -> None:
    response = client.post("/alerts", headers={"X-API-Key": KEY})
    assert response.status_code == 405 and response.headers["allow"] == "GET, HEAD"
    for method in ("put", "delete", "patch"):
        assert getattr(client, method)("/alerts").status_code == 405
    with_body = client.request(
        "GET", "/alerts", content=b"{}", headers={"X-API-Key": KEY, "Content-Type": "json"}
    )
    assert with_body.status_code == 413


def test_unknown_hosts_and_docs_are_refused(client: TestClient) -> None:
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert _get(client, path).status_code == 404


def test_queries_beyond_the_limit_are_shed(served: Settings) -> None:
    client = _client(_settings(served, max_concurrent_queries=1))
    held = client.app.state.api.queries
    with held:
        response = _get(client, "/alerts")
    assert response.status_code == 503 and response.headers["retry-after"] == "1"
    assert _get(client, "/alerts").status_code == 200


def test_errors_tell_nothing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("secret detail at /home/someone")

    monkeypatch.setattr(queries, "list_alerts", broken)
    response = _get(client, "/alerts")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_no_published_data_is_a_503(served: Settings, tmp_path: Path) -> None:
    batch = served.batch.model_copy(update={"serving_path": tmp_path / "none.duckdb"})
    client = _client(served.model_copy(update={"batch": batch}))
    assert client.get("/health").json()["data"] == "none yet"
    response = _get(client, "/alerts")
    assert response.status_code == 503 and response.json() == {"detail": "no data published yet"}


def _serving(settings: Settings, query: str) -> list[tuple]:
    with duckdb.connect(str(settings.batch.serving_path), read_only=True) as con:
        return con.execute(query).fetchall()


def test_alerts_page_through_the_queue(served: Settings, client: TestClient) -> None:
    ((total,),) = _serving(served, "SELECT count(*) FROM alerts")
    page = _get(client, "/alerts", limit=2, offset=1).json()
    assert page["total"] == total and page["limit"] == 2 and page["offset"] == 1
    everything = _get(client, "/alerts", limit=10_000).json()
    assert everything["limit"] == served.api.page_limit
    ids = [a["alert_id"] for a in everything["alerts"]]
    assert [a["alert_id"] for a in page["alerts"]] == ids[1:3]
    assert {a["phase"] for a in everything["alerts"]} <= {"train", "validation", "test"}

    model = _get(client, "/alerts", source="model").json()
    assert model["total"] > 0 and all("model" in a["sources"] for a in model["alerts"])
    day = _get(client, "/alerts", day="2022-09-07").json()
    assert day["total"] > 0 and {a["day"] for a in day["alerts"]} == {"2022-09-07"}


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"offset": -1}, {"source": "R1; DROP"}, {"day": "yesterday"}]
)
def test_bad_alert_queries_are_refused(client: TestClient, params: dict) -> None:
    assert _get(client, "/alerts", **params).status_code == 422


def test_a_case_shows_its_account_day(served: Settings, client: TestClient) -> None:
    alert = _get(client, "/alerts", limit=1).json()["alerts"][0]
    case = _get(client, f"/cases/{alert['alert_id']}").json()
    assert case["alert"] == alert
    assert case["investigation"] is None
    assert case["transactions"]
    for t in case["transactions"]:
        assert t["transacted_at"].startswith(alert["day"])
        assert alert["account_key"] in (t["sender_account_key"], t["receiver_account_key"])
    raised = {t["transaction_id"] for t in case["transactions"] if t["raised_alert"]}
    assert raised == set(alert["transaction_ids"]) & {
        t["transaction_id"] for t in case["transactions"]
    }


def test_a_case_shows_the_investigation_of_its_day(
    served: Settings, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`investigate_day` hands the agent the top of the day's queue, as the case sets show
    alerts, and keeps each investigation where `/cases` reads it."""
    day = date(2022, 9, 8)
    handed: list[tuple[list[CaseAlert], date | None]] = []

    async def agent(
        settings: Settings,
        alerts: Sequence[CaseAlert],
        on_result: Callable[[Investigation], None],
        batch_day: date | None = None,
    ) -> list[Investigation]:
        handed.append((list(alerts), batch_day))
        results = [_investigation(alert) for alert in alerts]
        for result in results:
            on_result(result)
        return results

    monkeypatch.setattr("atalayero.agent.run.investigate_alerts", agent)
    queue = _get(client, "/alerts", day=day.isoformat()).json()["alerts"]
    try:
        asyncio.run(investigate_day(served, day, 2))

        [(alerts, batch_day)] = handed
        assert batch_day == day
        assert [a.alert_id for a in alerts] == [a["alert_id"] for a in queue[:2]]
        assert all(type(a) is CaseAlert for a in alerts)  # not the day's phase
        investigation = _get(client, f"/cases/{queue[0]['alert_id']}").json()["investigation"]
        assert investigation["report"]["decision"] == "escalate"
        assert investigation["grounded"] is True and investigation["config_version"] == "1.4"
        assert _get(client, f"/cases/{queue[2]['alert_id']}").json()["investigation"] is None
    finally:
        shutil.rmtree(day_dir(served, day) / "investigations", ignore_errors=True)


def _investigation(alert: CaseAlert) -> Investigation:
    report = CaseReport(
        alert_id=alert.alert_id,
        decision="escalate",
        typology="unclassified",
        evidence=(Evidence(transaction_id=alert.transaction_ids[0], amount_usd=1.0),),
        confidence=0.7,
        narrative="Test.",
    )
    return Investigation(
        alert_id=alert.alert_id,
        report=report,
        grounding=Grounding(grounded=True, hallucinated_ids=0, problems=[]),
        steps=3,
        llm_calls=5,
        retries=0,
        tokens=100,
        seconds=2.0,
        config_version="1.4",
        prompts_sha256="x",
        transcript=[],
    )


@pytest.mark.parametrize(
    "alert_id", ["2022-09-07:999:ABC", "..%2F..%2Fetc%2Fpasswd", "2022-09-07:1:abc", "x" * 80]
)
def test_unknown_or_malformed_cases(client: TestClient, alert_id: str) -> None:
    assert _get(client, f"/cases/{alert_id}").status_code in (404, 422)


def test_scores_come_from_the_batch(served: Settings, client: TestClient) -> None:
    alert = _get(client, "/alerts", limit=1).json()["alerts"][0]
    raised = alert["transaction_ids"][0]
    score = _get(client, f"/score/{raised}").json()
    assert alert["alert_id"] in score["alert_ids"]
    assert score["champion_family"] and score["champion_version"]
    ((expected,),) = _serving(served, f"SELECT score FROM scores WHERE transaction_id = {raised}")
    assert score["score"] == pytest.approx(expected)
    with duckdb.connect(str(served.duckdb_path), read_only=True) as con:
        (warm_up,) = con.execute(  # the warm-up day is never scored
            "SELECT min(transaction_id) FROM marts.fct_transactions "
            "WHERE transacted_at < DATE '2022-09-06'"
        ).fetchone()
    assert _get(client, f"/score/{warm_up}").status_code == 404
    assert _get(client, "/score/999999999").status_code == 404
    assert _get(client, "/score/-1").status_code == 422


def test_the_token_bucket_refills_with_time() -> None:
    now = [0.0]
    bucket = TokenBucket(rate=1.0, capacity=2, clock=lambda: now[0])
    assert [bucket.take("k") for _ in range(2)] == [0.0, 0.0]
    assert bucket.take("k") == pytest.approx(1.0)
    now[0] = 0.5
    assert bucket.take("k") == pytest.approx(0.5)
    now[0] = 1.6
    assert bucket.take("k") == 0.0
    assert bucket.take("other") == 0.0  # each key has its own bucket
