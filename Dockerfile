FROM alpine:edge

WORKDIR /srv

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

RUN --mount=type=bind,source=pyproject.toml,target=/srv/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/srv/uv.lock \
    apk add --no-cache \
        gdal-driver-parquet \
        gdal-tools \
        python3 && \
    apk add --no-cache --virtual .build-deps \
        build-base \
        python3-dev \
        uv && \
    uv sync --frozen --no-dev --no-install-project && \
    apk del .build-deps && \
    python -c "import duckdb; conn = duckdb.connect(); conn.execute('INSTALL spatial;')"

COPY app ./app

ENTRYPOINT ["python", "-m", "app"]
