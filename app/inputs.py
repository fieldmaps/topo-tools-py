"""Imports geodata via GDAL, reprojects to EPSG:4326, and stores as Parquet."""

from logging import getLogger
from pathlib import Path
from subprocess import CalledProcessError, run

from duckdb import DuckDBPyConnection

from .config import GDAL_PARQUET_LCO
from .utils import parquet

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str, file: Path, layer: str) -> None:
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
        SELECT * EXCLUDE (geom)
        FROM '{attr_tmp}'
    """)

    # Pipeline: fix geometry, force 2D + multi, clean topology
    p01_tmp = parquet(f"{name}_01_tmp")
    try:
        # fmt: off
        try:
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
        except CalledProcessError:
            logger.warning(
                "%s contains non-polygon geometries after make-valid, "
                "source data has invalid features that cannot be topology-cleaned",
                name,
            )
            raise
        # fmt: on
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_01" AS
            SELECT * FROM '{p01_tmp}'
        """)
    finally:
        Path(attr_tmp).unlink(missing_ok=True)
        Path(p01_tmp).unlink(missing_ok=True)
