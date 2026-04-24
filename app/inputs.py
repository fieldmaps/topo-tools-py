"""Imports geodata via GDAL, reprojects to EPSG:4326, and stores as Parquet."""

from pathlib import Path
from subprocess import run

import duckdb

from .config import GDAL_PARQUET_LCO
from .utils import parquet


def main(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    file: Path,
    layer: str,
    *_: list,
) -> None:
    """Import geodata into DuckDB tables with topology cleaning."""
    attr_tmp = parquet(f"{name}_attr_tmp")

    # Import all columns (attributes only, no geometry processing needed here)
    # fmt: off
    run(
        [
            "gdal", "vector", "convert",
            str(file), f"--input-layer={layer}",
            attr_tmp, "--layer-creation-option=FID=fid", *GDAL_PARQUET_LCO,
        ],
        check=True,
    )
    # fmt: on

    # Write _attr table: all attributes without geometry
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_attr" AS
        SELECT * EXCLUDE (geometry)
        FROM read_parquet('{attr_tmp}')
    """)

    # Pipeline: fix geometry, force 2D + multi, clean topology
    p01_tmp = parquet(f"{name}_01_tmp")
    try:
        # fmt: off
        run(
            [
                "gdal", "vector", "pipeline",
                "read", attr_tmp,
                "!", "make-valid",
                "!", "reproject", "--dst-crs=EPSG:4326",
                "!", "set-geom-type", "--multi", "--dim=XY",
                "!", "clean-coverage",
                "!", "write", "--layer-creation-option=FID=fid",
                *GDAL_PARQUET_LCO, p01_tmp,
            ],
            check=True,
        )
        # fmt: on
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_01" AS
            SELECT * FROM read_parquet('{p01_tmp}')
        """)
    finally:
        Path(attr_tmp).unlink(missing_ok=True)
        Path(p01_tmp).unlink(missing_ok=True)
