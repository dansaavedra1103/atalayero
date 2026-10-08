from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Request

from atalayero.batch import queries
from atalayero.schemas import AlertPage

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
def list_alerts(
    request: Request,
    day: date | None = None,
    source: Annotated[str | None, Query(pattern=r"^(model|R\d{2})$")] = None,
    limit: Annotated[int, Query(ge=1)] = 100,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> AlertPage:
    """The alert queue by day and rank; `source` is a rule ID or `model`."""
    api = request.app.state.api
    limit = min(limit, api.settings.api.page_limit)
    with api.queries:
        return queries.list_alerts(api.settings, day, source, limit, offset)
