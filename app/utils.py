"""Shared utilities: DuckDB connection and pipeline helpers."""

import re
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from logging import getLogger

from duckdb import DuckDBPyConnection
from duckdb import connect as duckdb_connect

from .config import in_memory, num_threads, profile, tmp_dir

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
    """Proxy around DuckDBPyConnection; logs timing and memory delta per execute()."""

    def __init__(self, conn: DuckDBPyConnection, db_path: str | None = None) -> None:  # noqa: D107
        self._conn = conn
        # Second connection to the same file shares the buffer manager, allowing
        # peak memory to be sampled from a background thread during query execution.
        # Not available for in-memory databases (no file to connect to).
        self._monitor = duckdb_connect(db_path) if db_path and profile else None

    def execute(self, query: str, parameters: list | None = None):  # noqa: ANN201
        """Log wall-clock time and duckdb_memory() delta, then forward."""
        if not profile:
            return (
                self._conn.execute(query, parameters)
                if parameters is not None
                else self._conn.execute(query)
            )
        t0 = time.perf_counter()
        before = self._conn.execute(_MEM_Q).fetchone()[0]

        peak = [before]
        stop = threading.Event()

        def _poll() -> None:
            while not stop.is_set():
                try:
                    val = self._monitor.execute(_MEM_Q).fetchone()[0]
                    peak[0] = max(peak[0], val)
                except Exception:  # noqa: BLE001, S110
                    pass
                stop.wait(0.05)

        if self._monitor:
            threading.Thread(target=_poll, daemon=True).start()

        result = (
            self._conn.execute(query, parameters)
            if parameters is not None
            else self._conn.execute(query)
        )
        # Materialize before the after-memory query, which would otherwise
        # invalidate the result cursor on the same connection.
        rows = result.fetchall()

        stop.set()
        after = self._conn.execute(_MEM_Q).fetchone()[0]
        peak[0] = max(peak[0], after)

        elapsed = time.perf_counter() - t0
        if self._monitor:
            logger.info(
                "query %.3fs | %+.1f MB | %.1f MB total | %.1f MB peak | %s",
                elapsed,
                (after - before) / 1e6,
                after / 1e6,
                peak[0] / 1e6,
                _query_label(query),
            )
        else:
            logger.info(
                "query %.3fs | %+.1f MB | %.1f MB total | %s",
                elapsed,
                (after - before) / 1e6,
                after / 1e6,
                _query_label(query),
            )
        return _EagerResult(rows)

    def close(self) -> None:
        """Close the underlying connection."""
        if self._monitor:
            self._monitor.close()
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
    return ProfiledConnection(conn, db_path=db_path)


@contextmanager
def spatial_join_memory(conn: DuckDBPyConnection) -> Generator[None, None, None]:
    """Temporarily bypass DuckDB 1.5.2 SPATIAL_JOIN virtual-memory reservation.

    The SPATIAL_JOIN operator pre-allocates ~1x physical RAM as a virtual spill
    reservation. Raising memory_limit above that threshold lets it proceed;
    actual peak usage stays under 100 MB. See CLAUDE.md for full details.
    """
    orig = conn.execute("SELECT current_setting('memory_limit')").fetchall()[0][0]
    conn.execute("SET memory_limit = '999GB'")
    try:
        yield
    finally:
        conn.execute(f"SET memory_limit = '{orig}'")


def cleanup_tmp(name: str, *, parquet: bool = False) -> None:
    """Remove tmp files for a named pipeline run."""
    if not in_memory:
        for p in tmp_dir.glob(f"{name}.duckdb*"):
            p.unlink(missing_ok=True)
    if parquet:
        for p in tmp_dir.glob(f"{name}*.parquet"):
            p.unlink(missing_ok=True)


def export_debug_tables(conn: DuckDBPyConnection) -> None:
    """Export all pipeline tables to Parquet files for inspection."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    for (table,) in tables:
        out = str(tmp_dir / f"{table}.parquet")
        has_geom = conn.execute(
            "SELECT COUNT(*) > 0 FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND data_type = 'GEOMETRY'"
        ).fetchall()[0][0]
        opts = _GEO_PARQUET if has_geom else _PARQUET
        conn.execute(f"COPY \"{table}\" TO '{out}' {opts}")
