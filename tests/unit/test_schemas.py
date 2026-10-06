from collections.abc import Callable

import pydantic
import pytest

from atalayero.schemas import CaseAlert, CaseReport, Transaction

MakeTx = Callable[..., Transaction]


def test_transaction_survives_a_json_round_trip(make_tx: MakeTx) -> None:
    tx = make_tx(1, 0, usd=0.025852)

    assert Transaction.model_validate_json(tx.model_dump_json()) == tx


def test_transaction_rejects_a_label(make_tx: MakeTx) -> None:
    fields = make_tx(1, 0).model_dump()

    with pytest.raises(pydantic.ValidationError, match="is_laundering"):
        Transaction(**fields, is_laundering=True)


def test_a_report_closes_with_no_typology_and_only_then() -> None:
    fields = {"alert_id": "a", "evidence": (), "confidence": 0.5, "narrative": ""}

    CaseReport(**fields, decision="escalate", typology="unclassified")
    CaseReport(**fields, decision="close", typology="none")
    for decision, typology in [("close", "cycle"), ("escalate", "none")]:
        with pytest.raises(pydantic.ValidationError, match="closes with typology 'none'"):
            CaseReport(**fields, decision=decision, typology=typology)


def test_an_alert_carries_no_label() -> None:
    fields = {
        "alert_id": "2022-09-09:001:A",
        "account_key": "001:A",
        "day": "2022-09-09",
        "sources": ("model",),
        "score": 0.9,
        "rank": 1,
        "transaction_ids": (1,),
    }

    CaseAlert(**fields)
    with pytest.raises(pydantic.ValidationError, match="decision"):
        CaseAlert(**fields, decision="escalate")
