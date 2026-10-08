# Airflow with the project's own virtual environment beside it (ADR-0021). Tasks run the package's
# commands with /opt/atalayero/venv, so the project's pinned dependencies never mix with Airflow's.
# The code, config and data are not in the image: the repository is mounted at run time.
FROM apache/airflow:3.3.2-python3.11

USER root
# LightGBM needs the OpenMP runtime (ADR-0009).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
RUN mkdir -p /opt/atalayero && chown airflow:0 /opt/atalayero

USER airflow
ENV UV_PROJECT_ENVIRONMENT=/opt/atalayero/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /opt/atalayero/build
COPY --chown=airflow:0 pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python3.11 \
    && rm -rf /home/airflow/.cache/uv
WORKDIR /opt/airflow
