"""Shared utilities: DuckDB connection and pipeline helpers."""

import contextlib
import re
import threading
import time
from logging import getLogger

import psutil
from duckdb import DuckDBPyConnection
from duckdb import connect as duckdb_connect

from .config import in_memory, num_threads, profile, tmp_dir

_PROCESS = psutil.Process()
logger = getLogger(__name__)

_GEO_PARQUET = (
    "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2')"
)
_PARQUET = "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15)"
_MEM_Q = "SELECT COALESCE(SUM(memory_usage_bytes), 0) FROM duckdb_memory()"


def _query_label(query: str) -> str:
    q = " ".join(query.split())
    m = re.search(r'CREATE (?:OR REPLACE )?TABLE "([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"CREATE {m.group(1)}"
    m = re.search(r'DROP TABLE (?:IF EXISTS )?"([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"DROP {m.group(1)}"
    m = re.search(r'ALTER TABLE "[^"]+" RENAME TO "([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"RENAME TO {m.group(1)}"
    m = re.search(r"COPY .+? TO '([^']+)'", q, re.IGNORECASE)
    if m:
        return f"COPY {m.group(1)}"
    return q[:80]


class _EagerResult:
    """Materialized DuckDB result that survives subsequent execute() calls."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self._idx = 0

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self) -> tuple | None:
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None


class ProfiledConnection:
    """Proxy around DuckDBPyConnection; logs RSS peak and duckdb delta per execute().

    RSS peak is the primary metric for Docker/WASM sizing — it captures GEOS working
    memory (ST_VoronoiDiagram, ST_Node, ST_Polygonize) that duckdb_memory() misses.
    duckdb delta/total are shown as secondary context for table accumulation.
    """

    def __init__(self, conn: DuckDBPyConnection) -> None:  # noqa: D107
        self._conn = conn

    def execute(self, query: str, parameters: list | None = None):  # noqa: ANN201
        """Log wall-clock time, RSS peak, and duckdb delta/total, then forward."""
        if not profile:
            return (
                self._conn.execute(query, parameters)
                if parameters is not None
                else self._conn.execute(query)
            )
        t0 = time.perf_counter()
        before_rss = _PROCESS.memory_info().rss
        before_ddb = self._conn.execute(_MEM_Q).fetchall()[0][0]

        peak_rss = [before_rss]
        stop = threading.Event()

        def _poll() -> None:
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    peak_rss[0] = max(peak_rss[0], _PROCESS.memory_info().rss)
                stop.wait(0.05)

        threading.Thread(target=_poll, daemon=True).start()

        result = (
            self._conn.execute(query, parameters)
            if parameters is not None
            else self._conn.execute(query)
        )
        # Materialize before the after-memory queries, which would otherwise
        # invalidate the result cursor on the same connection.
        rows = result.fetchall()

        stop.set()
        after_rss = _PROCESS.memory_info().rss
        peak_rss[0] = max(peak_rss[0], after_rss)
        after_ddb = self._conn.execute(_MEM_Q).fetchall()[0][0]

        elapsed = time.perf_counter() - t0
        logger.info(
            "query %.3fs | rss peak %.0f MB | duckdb %+.0f MB | %.0f MB total | %s",
            elapsed,
            peak_rss[0] / 1e6,
            (after_ddb - before_ddb) / 1e6,
            after_ddb / 1e6,
            _query_label(query),
        )
        return _EagerResult(rows)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __getattr__(self, name: str) -> object:  # noqa: D105
        return getattr(self._conn, name)


def get_connection(name: str) -> ProfiledConnection:
    """Create a DuckDB connection (file-backed or in-memory) with spatial loaded."""
    db_path = None if in_memory else str(tmp_dir / f"{name}.duckdb")
    conn = duckdb_connect() if db_path is None else duckdb_connect(db_path)
    conn.execute("LOAD spatial")
    conn.execute("SET enable_progress_bar = false")
    conn.execute("SET geometry_always_xy = true")
    conn.execute("SET preserve_insertion_order = false")
    conn.execute(f"SET threads = {num_threads}")
    return ProfiledConnection(conn)


def cleanup_tmp(name: str, *, parquet: bool = False) -> None:
    """Remove tmp files for a named pipeline run."""
    if not in_memory:
        for p in tmp_dir.glob(f"{name}.duckdb*"):
            p.unlink(missing_ok=True)
    if parquet:
        for p in tmp_dir.glob(f"{name}*.parquet"):
            p.unlink(missing_ok=True)


def export_debug_tables(conn: DuckDBPyConnection, only: set[str] | None = None) -> None:
    """Export pipeline tables to Parquet files for inspection."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    for (table,) in tables:
        if only is not None and table not in only:
            continue
        out = str(tmp_dir / f"{table}.parquet")
        has_geom = conn.execute(
            "SELECT COUNT(*) > 0 FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND data_type = 'GEOMETRY'"
        ).fetchall()[0][0]
        opts = _GEO_PARQUET if has_geom else _PARQUET
        conn.execute(f"COPY \"{table}\" TO '{out}' {opts}")
