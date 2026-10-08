from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, status

from atalayero.batch import queries
from atalayero.schemas import Case

router = APIRouter(tags=["cases"])


@router.get("/cases/{alert_id}")
def get_case(
    request: Request,
    alert_id: Annotated[str, Path(pattern=queries.ALERT_ID.pattern, max_length=64)],
) -> Case:
    """An alert with every transaction of its account-day and the agent's investigation, if
    any."""
    api = request.app.state.api
    with api.queries:
        case = queries.get_case(api.settings, alert_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such alert")
    return case
