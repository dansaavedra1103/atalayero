"""The API's own defences (ADR-0022), behind those of the gateway.

- **API keys**, compared by their SHA-256 against the configured digests, in constant time. The
  API never holds a key, only digests, and refuses to start without one.
- **Rate limits**: a token bucket per key, and one per client for failed attempts at a key.
- **Load shedding**: at most `max_concurrent_queries` queries at once; beyond, 503 at once.
- **No bodies, no writes**: the API reads; any request with a body, or a method other than GET
  or HEAD, is refused before it reaches a route.
- **Headers** that keep browsers from sniffing, framing or caching what it returns, and an ID on
  every response to find it in the logs.
- **Errors say nothing**: an unexpected error is logged with its trace and answered with a bare
  500.
"""

import hashlib
import hmac
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}
READ_METHODS = {"GET", "HEAD"}


def key_digest(key: str) -> str:
    """The SHA-256 of an API key, as configured in `api.key_hashes`."""
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass
class TokenBucket:
    """`rate` tokens a second up to `capacity`, per key; a request takes one."""

    rate: float
    capacity: int
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> tokens, at
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, key: str) -> float:
        """0 if the request may go ahead; otherwise the seconds until it could."""
        with self._lock:
            now = self.clock()
            tokens, at = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - at) * self.rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1 - tokens) / self.rate


def too_many(wait: float) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "too many requests",
        headers={"Retry-After": str(max(1, round(wait)))},
    )


class ApiKeys:
    """Checks the `X-API-Key` header; a client that keeps failing is slowed down."""

    def __init__(self, digests: tuple[str, ...], failures: TokenBucket) -> None:
        if not digests:
            raise ValueError("no API key configured (api.key_hashes): the API would be open")
        self._digests = tuple(d.lower() for d in digests)
        self._failures = failures

    def key_id(self, request: Request) -> str:
        """A short ID of the caller's key, for rate limits and logs; fails with 401 or 429."""
        client = request.client.host if request.client else "unknown"
        offered = request.headers.get("X-API-Key", "")
        digest = key_digest(offered)
        # Compare with every digest, in constant time each, so timing tells nothing.
        valid = [hmac.compare_digest(digest, d) for d in self._digests]
        if offered and any(valid):
            return digest[:12]
        wait = self._failures.take(f"failed:{client}")
        logger.warning("Refused an API key from %s", client)
        if wait:
            raise too_many(wait)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "a valid API key is required",
            headers={"WWW-Authenticate": "ApiKey"},
        )


class QueryLimit:
    """At most `size` queries at once; the next one is told to come back instead of queueing."""

    def __init__(self, size: int) -> None:
        self._slots = threading.BoundedSemaphore(size)

    def __enter__(self) -> "QueryLimit":
        if not self._slots.acquire(blocking=False):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "busy, try again", headers={"Retry-After": "1"}
            )
        return self

    def __exit__(self, *_: object) -> None:
        self._slots.release()


class ReadOnlyGuard:
    """ASGI middleware: refuses bodies and other methods than GET and HEAD, adds the security
    headers and a request ID to every response, and logs each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        request_id = headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        started = time.monotonic()
        state = {"status": 0}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                extra = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                extra.append((b"x-request-id", request_id.encode()))
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        length = headers.get("content-length", "0")
        refusal = None
        if scope["method"] not in READ_METHODS:
            refusal = (405, b'{"detail":"the API only reads"}', [(b"allow", b"GET, HEAD")])
        elif not length.isdigit():
            refusal = (400, b'{"detail":"bad Content-Length"}', [])
        elif int(length) > 0 or "transfer-encoding" in headers:
            refusal = (413, b'{"detail":"requests carry no body"}', [])
        if refusal is None:
            try:
                await self.app(scope, receive, send_with_headers)
            except Exception:
                # The trace stays in the logs; the caller learns nothing from the error.
                logger.exception("Unhandled error, request %s", request_id)
                if state["status"]:  # the response had started: nothing more can be sent
                    raise
                await send_with_headers(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send_with_headers(
                    {"type": "http.response.body", "body": b'{"detail":"internal error"}'}
                )
        else:
            code, body, more = refusal
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": [(b"content-type", b"application/json"), *more],
                }
            )
            await send_with_headers({"type": "http.response.body", "body": body})
        logger.info(
            "%s %s %d %.0f ms request %s",
            scope["method"],
            scope["path"],
            state["status"],
            (time.monotonic() - started) * 1000,
            request_id,
        )
