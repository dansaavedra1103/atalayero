"""The API application (ADR-0022): read-only, keyed, rate-limited, behind the gateway."""

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from atalayero.api.routers import alerts, cases, health, scores
from atalayero.api.security import ApiKeys, QueryLimit, ReadOnlyGuard, TokenBucket, too_many
from atalayero.batch.queries import ServingUnavailableError
from atalayero.settings import Settings

logger = logging.getLogger(__name__)
FAILED_KEYS_PER_MINUTE = 5  # attempts with a wrong key, per client, before 429


@dataclass
class ApiState:
    settings: Settings
    keys: ApiKeys
    requests: TokenBucket
    queries: QueryLimit


def state(request: Request) -> ApiState:
    return request.app.state.api


def caller(request: Request, api: Annotated[ApiState, Depends(state)]) -> str:
    """The caller's key ID, once its key checks out and its rate allows the request."""
    key_id = api.keys.key_id(request)
    wait = api.requests.take(key_id)
    if wait:
        raise too_many(wait)
    return key_id


def create_app(settings: Settings) -> FastAPI:
    config = settings.api
    app = FastAPI(
        title="Atalayero API",
        summary="The alert queue, its cases and scores of the daily batch; synthetic data only.",
        docs_url="/docs" if config.docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs else None,
    )
    app.state.api = ApiState(
        settings=settings,
        keys=ApiKeys(
            config.key_hashes,
            failures=TokenBucket(rate=FAILED_KEYS_PER_MINUTE / 60, capacity=FAILED_KEYS_PER_MINUTE),
        ),
        requests=TokenBucket(rate=config.requests_per_minute / 60, capacity=config.burst),
        queries=QueryLimit(config.max_concurrent_queries),
    )
    keyed = [Depends(caller)]
    app.include_router(health.router)
    app.include_router(alerts.router, dependencies=keyed)
    app.include_router(cases.router, dependencies=keyed)
    app.include_router(scores.router, dependencies=keyed)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))
    app.add_middleware(ReadOnlyGuard)  # outermost: it sees every request and response

    @app.exception_handler(ServingUnavailableError)
    async def unavailable(request: Request, error: ServingUnavailableError) -> JSONResponse:
        logger.warning("Serving database unavailable: %s", error)
        return JSONResponse(
            {"detail": "no data published yet"},
            status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "60"},
        )

    return app
