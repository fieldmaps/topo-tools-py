FROM python:3.14-slim AS geos-builder

ARG GEOS_VERSION=3.14.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://download.osgeo.org/geos/geos-${GEOS_VERSION}.tar.bz2" \
        | tar xj -C /tmp \
    && cmake -S "/tmp/geos-${GEOS_VERSION}" -B /tmp/geos-build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/geos \
        -DBUILD_TESTING=OFF \
        -DBUILD_DOCUMENTATION=OFF \
    && cmake --build /tmp/geos-build -j"$(nproc)" \
    && cmake --install /tmp/geos-build


FROM python:3.14-slim

WORKDIR /srv

ENV PYTHONUNBUFFERED=1 \
    PATH="/srv/.venv/bin:$PATH"

COPY --from=geos-builder /opt/geos/lib/ /usr/local/lib/
RUN ldconfig

COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
    uv sync --frozen --no-dev --no-install-project && \
    python -c "import duckdb; duckdb.connect().execute('INSTALL spatial')"

COPY app ./app

ENTRYPOINT ["python", "-m", "app"]
