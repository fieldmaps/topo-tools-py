"""Shared utilities: DuckDB connection, coverage cleaning, and pipeline chaining."""

from logging import getLogger
from pathlib import Path
from subprocess import run

from duckdb import DuckDBPyConnection
from duckdb import connect as duckdb_connect

from .config import GDAL_PARQUET_LCO, PARQUET_OPTS, tmp_dir

logger = getLogger(__name__)


def get_connection(name: str) -> DuckDBPyConnection:
    """Create a file-backed DuckDB connection with the spatial extension loaded."""
    conn = duckdb_connect(str(tmp_dir / f"{name}.duckdb"))
    conn.execute("LOAD spatial")
    conn.execute("SET enable_progress_bar = false")
    conn.execute("SET geometry_always_xy = true")
    conn.execute("SET preserve_insertion_order = false")
    conn.execute("SET threads = 4")
    return conn


def parquet(name: str) -> str:
    """Return the path for an intermediate Parquet file."""
    return str(tmp_dir / f"{name}.parquet")


def coverage_clean(
    conn: DuckDBPyConnection,
    input_table: str,
    output_table: str,
) -> None:
    """Topology cleanup using GDAL clean-coverage.

    Snaps shared boundaries and eliminates gaps/overlaps between polygons,
    equivalent to PostGIS ST_CoverageClean(geometry) OVER ().
    """
    in_path = parquet(f"_{input_table}")
    out_path = parquet(f"_{output_table}")
    conn.execute(
        f"COPY (SELECT * FROM \"{input_table}\") TO '{in_path}' {PARQUET_OPTS}"
    )
    try:
        result = run(
            ["gdal", "vector", "clean-coverage", in_path, out_path, *GDAL_PARQUET_LCO],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            msg = f"coverage_clean failed: {result.stderr.decode()}"
            raise RuntimeError(msg)
        conn.execute(
            f"CREATE OR REPLACE TABLE \"{output_table}\" AS SELECT * FROM '{out_path}'"
        )
    finally:
        Path(in_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def cleanup_tmp(name: str) -> None:
    """Remove the DuckDB file and any GDAL interop Parquet files for a pipeline run."""
    for p in tmp_dir.glob(f"{name}.duckdb*"):
        p.unlink(missing_ok=True)
    for p in tmp_dir.glob(f"{name}_*.parquet"):
        p.unlink(missing_ok=True)
