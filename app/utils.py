import re
import sqlite3
from logging import getLogger
from pathlib import Path
from subprocess import PIPE, run

import duckdb

from .config import quiet, tmp_dir

logger = getLogger(__name__)

_PARQUET_OPTS = "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15)"


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
    equivalent to PostGIS ST_CoverageClean(geom) OVER ().
    """
    result = run(
        [
            "gdal",
            "vector",
            "clean-coverage",
            input_path,
            output_path,
            "--overwrite",
            "--quiet",
            "--lco=GEOMETRY_NAME=geom",
            "--lco=COMPRESSION=ZSTD",
            "--lco=COMPRESSION_LEVEL=15",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        msg = f"coverage_clean failed: {result.stderr.decode()}"
        raise RuntimeError(msg)


def apply_funcs(name: str, file: Path, layer: str, *args: list) -> None:
    """Apply pipeline functions using a shared DuckDB connection."""
    tmp_dir.mkdir(exist_ok=True, parents=True)
    conn = get_connection()
    total = len(args)
    for i, func in enumerate(args, 1):
        if not quiet:
            step = func.__module__.split(".")[-1]
            logger.info("%s: step %s/%s (%s)", name, i, total, step)
        func(conn, name, file, layer)
    conn.close()
