"""CLI: python -m atalayero.api — serve the API with uvicorn (ADR-0022).

It listens on every interface of its container, which only the gateway can reach, and trusts the
gateway's X-Forwarded-For for the client's address."""

import logging

import uvicorn

from atalayero.api.app import create_app
from atalayero.settings import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(
        create_app(Settings()),
        host="0.0.0.0",  # noqa: S104 (inside the container; only the gateway reaches it)
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*",  # only the gateway can connect (internal network)
        server_header=False,
        date_header=False,
        timeout_keep_alive=5,
        limit_concurrency=64,  # connections; beyond, 503
        access_log=False,  # ReadOnlyGuard logs every request, with its ID
    )


if __name__ == "__main__":
    main()
