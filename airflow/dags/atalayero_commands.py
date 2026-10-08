"""How the DAGs run the project (ADR-0021): only the package's own commands, never its logic.

The repository is mounted at the same path as on the host (`ATALAYERO_ROOT`), so that the paths
MLflow recorded resolve, and commands run there with the project's own virtual environment, built
into the image from `uv.lock`. Nothing here imports `atalayero`: Airflow parses the DAGs with its
own Python.
"""

import os

ROOT = os.environ.get("ATALAYERO_ROOT", "/opt/atalayero/repo")
VENV = "/opt/atalayero/venv/bin"
ENV = {
    "PYTHONPATH": f"{ROOT}/src",
    "PYTHONDONTWRITEBYTECODE": "1",  # the repository is mounted read-only
    "MLFLOW_DISABLE_AGENT_HINT": "1",
}


def package(*args: str) -> str:
    """A command of the package, e.g. `package("atalayero.batch", "publish")`."""
    return " ".join([f"{VENV}/python", "-m", *args])


def dbt(*args: str) -> str:
    """A dbt command on the project; its target and logs go to /tmp (the repository is
    read-only)."""
    return " ".join(
        [
            f"{VENV}/dbt",
            *args,
            "--project-dir dbt --profiles-dir dbt",
            "--target-path /tmp/dbt-target --log-path /tmp/dbt-logs",
        ]
    )
