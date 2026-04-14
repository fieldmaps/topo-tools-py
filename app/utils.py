import logging
import re
import sqlite3
from pathlib import Path
from subprocess import PIPE, run

import duckdb

from .config import GDAL_PARQUET_LCO, tmp_dir

logger = logging.getLogger(__name__)


def get_gpkg_layers(file: Path) -> list[str]:
    """Get list of layers in GeoPackage."""
    query = """
        SELECT table_name, geometry_type_name
        FROM gpkg_geometry_columns
        WHERE geometry_type_name IN ('POLYGON', 'MULTIPOLYGON', 'GEOMETRY');
    """
    con = sqlite3.connect(file)
    cur = con.cursor()
    layers = sorted([row[0] for row in cur.execute(query)])
    cur.close()
    con.close()
    return layers


def is_polygon(file: Path) -> bool:
    """Check if file is a polygon."""
    regex = re.compile(r"\((?:Multi )?Polygon\)")
    result = run(
        ["gdal", "vector", "info", "--summary", file],
        check=False,
        stdout=PIPE,
    )
    return bool(regex.search(str(result.stdout)))


def get_connection() -> duckdb.DuckDBPyConnection:
    """Create a DuckDB in-memory connection with the spatial extension loaded."""
    conn = duckdb.connect()
    conn.execute("LOAD spatial;")
    conn.execute("SET geometry_always_xy = true;")
    conn.execute("SET enable_progress_bar = false;")
    return conn


def parquet(name: str) -> str:
    """Return the path for an intermediate Parquet file."""
    return str(tmp_dir / f"{name}.parquet")


def coverage_clean(input_path: str, output_path: str) -> None:
    """Topology cleanup using GDAL clean-coverage.

    Snaps shared boundaries and eliminates gaps/overlaps between polygons,
    equivalent to PostGIS ST_CoverageClean(geometry) OVER ().
    """
    result = run(
        [
            "gdal",
            "vector",
            "clean-coverage",
            input_path,
            output_path,
            *GDAL_PARQUET_LCO,
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        msg = f"coverage_clean failed: {result.stderr.decode()}"
        raise RuntimeError(msg)


def cleanup_tmp(name: str) -> None:
    """Remove all intermediate Parquet files for a given pipeline run."""
    for p in tmp_dir.glob(f"{name}_*.parquet"):
        p.unlink(missing_ok=True)


def apply_funcs(name: str, file: Path, layer: str, *args: list) -> None:
    """Apply pipeline functions using a shared DuckDB connection."""
    tmp_dir.mkdir(exist_ok=True, parents=True)
    cleanup_tmp(name)
    conn = get_connection()
    try:
        for func in args:
            func(conn, name, file, layer)
        logger.info("done: %s", name)
    finally:
        conn.close()
        cleanup_tmp(name)
