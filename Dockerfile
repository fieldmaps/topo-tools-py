FROM alpine:edge

WORKDIR /srv

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache \
        -X https://dl-cdn.alpinelinux.org/alpine/edge/testing \
        gdal-driver-parquet \
        gdal-tools \
        py3-duckdb \
        python3 && \
    python -m venv --system-site-packages /opt/venv && \
    python -c "import duckdb; conn = duckdb.connect(); conn.execute('INSTALL spatial')"

COPY app ./app

ENTRYPOINT ["python", "-m", "app"]
