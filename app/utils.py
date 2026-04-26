"""Shared utilities: DuckDB connection and pipeline helpers."""

from collections.abc import Generator
from contextlib import contextmanager

from duckdb import DuckDBPyConnection
from duckdb import connect as duckdb_connect

from .config import num_threads, tmp_dir


def get_connection(name: str) -> DuckDBPyConnection:
    """Create a file-backed DuckDB connection with the spatial extension loaded."""
    conn = duckdb_connect(str(tmp_dir / f"{name}.duckdb"))
    conn.execute("LOAD spatial")
    conn.execute("SET enable_progress_bar = false")
    conn.execute("SET geometry_always_xy = true")
    conn.execute("SET preserve_insertion_order = false")
    conn.execute(f"SET threads = {num_threads}")
    return conn


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


def cleanup_tmp(name: str) -> None:
    """Remove the DuckDB file for a pipeline run."""
    for p in tmp_dir.glob(f"{name}.duckdb*"):
        p.unlink(missing_ok=True)
