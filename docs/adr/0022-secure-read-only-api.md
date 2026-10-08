# ADR-0022: A read-only API behind a gateway

- Status: accepted
- Date: 2026-10-08

## Context

- **design.md §6 asks for an API** with `/score`, `/alerts` and `/cases/{id}`, started with
  `docker compose up`.
- **Its security is a requirement**: rate limiting, a gateway, throttling, and whatever else an
  exposed API needs. Everything stays local and free.
- **The batch publishes what the API needs** in the serving database, read-only, with no labels
  (ADR-0020).
- **Scoring a new transaction on demand was the first choice for `/score`**, with a fallback to
  the batch's stored scores if it took more than about 5 seconds a transaction. It does. Its
  point-in-time features need the motif state at the start of its day, and loading that state
  alone takes about 12 seconds (ADR-0020). The day's graph snapshot comes on top.

## Decision

1. **The API only reads.** It has four routes, all GET, over the serving database and nothing
   else:

   | Route | Returns |
   | --- | --- |
   | `/health` | Whether it is up and has data; no key needed |
   | `/alerts` | The queue by day and rank, filtered by day or source, in pages of up to 500 |
   | `/cases/{alert_id}` | An alert, every transaction of its account-day with its score, and the agent's investigation if one ran (ADR-0024) |
   | `/score/{transaction_id}` | The score the batch gave the transaction, the champion version behind it, and the alerts it raised |

   Responses use the shared schemas (`QueuedAlert`, `Case`, `TransactionScore`…). No route
   computes a score: the API serves what the batch decided.
2. **A gateway is the only way in.** nginx, in its unprivileged image, listens on
   `127.0.0.1:8000`. The API sits on an internal Docker network with no published port and no
   way out. The gateway:
   - **throttles each client**: 10 requests a second, a burst of 20 more held back to that pace,
     and 429 beyond;
   - caps each client at 20 connections at once;
   - refuses methods other than GET and HEAD, and bodies over 1 KB;
   - times out slow clients (5 s for headers and body) and a slow API (2 s to connect, 15 s to
     answer);
   - **replaces** `X-Forwarded-For` instead of appending to it, so that a client cannot choose
     the address the API rate-limits;
   - stamps a request ID, sets the security headers on every response (its own error pages
     included), and hides its version.
3. **The API defends itself as well**, in case the gateway is bypassed or misconfigured:
   - **API keys.** A client sends `X-API-Key`. The API is configured with the SHA-256 of each
     accepted key, never the key, and compares digests in constant time. It refuses to start
     without one.
   - **Rate limits.** A token bucket per key allows 60 requests a minute with bursts of 20, and
     answers 429 with `Retry-After` beyond. A client that offers wrong keys gets 5 tries a
     minute, then 429.
   - **Load shedding.** At most 4 queries run at once; the next gets 503 with `Retry-After: 1`
     instead of a queue. uvicorn takes at most 64 connections and keeps an idle one for 5 s.
   - **Strict input.** Each path and query parameter has a pattern or a range. An alert ID must
     look like one before it goes near a file name, which closes path traversal.
   - **No writes, no bodies.** Other methods get 405; a request with a body gets 413.
   - **Only known hosts** (`localhost`, `127.0.0.1`, `api`): any other `Host` header gets 400.
   - **Errors say nothing.** An unexpected error is logged with its trace and answered with a bare
     500. A missing serving database is a 503. `/docs` and `/openapi.json` are off unless
     `api.docs` is set.
   - **Headers and logs.** Every response carries `nosniff`, `DENY`, a `default-src 'none'`
     policy, `no-referrer` and `no-store`, plus the request ID. Each request is logged with its
     ID, status and time, never with a key.
4. **Hardened containers.** The API runs as an unprivileged user (10001), and nginx as nginx's.
   Both have read-only root filesystems with a tmpfs for `/tmp`, drop every Linux capability,
   forbid new privileges, and run under memory, CPU and process limits. The API mounts the
   repository read-only. Images are pinned by version, and the API's dependencies by `uv.lock`.
5. **Secrets.** `make up` (`docker/local_env.py`) adds an API key and its SHA-256 to `.env`. It
   adds what is missing and never changes a value. `.env` is readable by its owner only and is
   never committed. The API container receives only the digest.
6. **Checks.** Tests cover every defence of the API. CI builds the API image, imports the app in
   it read-only, and validates the gateway's configuration with `nginx -t`.

## Consequences

- **No scoring on demand.** `/score` answers for transactions the batch has scored, and 404 for
  any other. Scoring live would take a feature service that keeps each day's motif state in
  memory.
- **Rate limits live in each API process.** One replica runs here. More would need a shared
  store, such as Redis, for the buckets, and they all reset when the API restarts.
- **No TLS.** Everything listens on localhost. Beyond the machine, the gateway would terminate
  TLS and send HSTS.
- **Static keys, not users.** There are no accounts, scopes or expiry. A key is revoked by
  removing its digest from `.env` and restarting the API.
- **The image carries the whole project environment** (LightGBM, MLflow…), though the API
  imports only DuckDB, Pydantic and FastAPI. That keeps one lock file, at the cost of size.
