FROM python:3.13-slim

WORKDIR /srv

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir duckdb==1.5.2 && \
    python -c "import duckdb; conn = duckdb.connect(); conn.execute('INSTALL spatial')"

COPY app ./app

ENTRYPOINT ["python", "-m", "app"]
