"""python -m atalayero.mcp_server: serve the investigator's tools over stdio."""

import logging
import os

from atalayero.mcp_server.server import main

if __name__ == "__main__":
    # MLflow's progress bars and hints are noise on a server; logs go to stderr, as stdout
    # carries the protocol.
    os.environ.setdefault("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    logging.basicConfig(level=logging.WARNING)
    main()
