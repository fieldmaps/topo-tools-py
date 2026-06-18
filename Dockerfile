FROM python:3.14-slim

WORKDIR /srv

ENV PYTHONUNBUFFERED=1 \
    PATH="/srv/.venv/bin:$PATH"

RUN --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    uv sync --frozen --no-dev --no-install-project && \
    python -c "import duckdb; duckdb.connect().execute('INSTALL spatial')"

COPY app ./app

ENTRYPOINT ["python", "-m", "app"]
