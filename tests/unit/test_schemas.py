from collections.abc import Callable

import pydantic
import pytest

from atalayero.schemas import Transaction

MakeTx = Callable[..., Transaction]


def test_transaction_survives_a_json_round_trip(make_tx: MakeTx) -> None:
    tx = make_tx(1, 0, usd=0.025852)

    assert Transaction.model_validate_json(tx.model_dump_json()) == tx


def test_transaction_rejects_a_label(make_tx: MakeTx) -> None:
    fields = make_tx(1, 0).model_dump()

    with pytest.raises(pydantic.ValidationError, match="is_laundering"):
        Transaction(**fields, is_laundering=True)
