from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from atalayero.batch import queries
from atalayero.schemas import TransactionScore

router = APIRouter(tags=["scores"])


@router.get("/score/{transaction_id}")
def get_score(
    request: Request, transaction_id: Annotated[int, Path(ge=0, le=2**53)]
) -> TransactionScore:
    """The score the batch gave a transaction of a complete day, and the champion behind it.
    Scoring a new transaction is not offered: its point-in-time features take the day's motif
    state, about 12 s to load (ADR-0022)."""
    api = request.app.state.api
    with api.queries:
        score = queries.get_score(api.settings, transaction_id)
    if score is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no score for that transaction")
    return score
