# The API (ADR-0022): the project's environment on a slim Python, run by an unprivileged user.
# The code, config and data are not in the image: the repository is mounted read-only at run time.
FROM python:3.11.17-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/atalayero/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/atalayero/build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python3.11 \
    && rm -rf /root/.cache/uv /usr/local/bin/uv \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin api

USER 10001
ENTRYPOINT ["/opt/atalayero/venv/bin/python", "-m", "atalayero.api"]
