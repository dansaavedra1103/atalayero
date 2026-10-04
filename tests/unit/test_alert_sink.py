from datetime import datetime
from pathlib import Path

import duckdb

from atalayero.schemas import Alert
from atalayero.streaming.consumer import AlertSink


def _alert(transaction_id: int) -> Alert:
    return Alert(
        alert_id=f"R04:{transaction_id}",
        rule_id="R04",
        rule_version="1.0",
        account_key="001:A",
        triggered_at=datetime(2022, 9, 5, 9, transaction_id),
        transaction_id=transaction_id,
        value=10,
        evidence=(transaction_id, transaction_id - 1),
    )


def _read(alerts_dir: Path) -> list[Alert]:
    rel = duckdb.sql(f"SELECT * FROM read_parquet('{alerts_dir}/part-*.parquet') ORDER BY 1")
    return [Alert(**dict(zip(rel.columns, row, strict=True))) for row in rel.fetchall()]


def test_writes_alerts_in_parquet_parts(tmp_path: Path) -> None:
    alerts = [_alert(i) for i in (11, 12, 13)]
    sink = AlertSink(tmp_path, batch_size=2)

    for alert in alerts:  # the second fills a batch and is flushed; the third waits
        sink.extend([alert])
    assert sink.written == 2
    sink.flush()

    assert sink.written == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "part-00000.parquet",
        "part-00001.parquet",
    ]
    assert _read(tmp_path) == alerts


def test_a_new_run_replaces_only_the_previous_parts(tmp_path: Path) -> None:
    first = AlertSink(tmp_path)
    first.extend([_alert(11), _alert(12)])
    first.flush()
    (tmp_path / "notes.txt").write_text("not an alert part")

    second = AlertSink(tmp_path)
    second.extend([_alert(13)])
    second.flush()

    assert _read(tmp_path) == [_alert(13)]
    assert (tmp_path / "notes.txt").exists()
