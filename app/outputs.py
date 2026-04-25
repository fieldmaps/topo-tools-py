"""Joins geometry with original attributes and exports output via GDAL."""

from logging import getLogger
from pathlib import Path
from subprocess import run
from time import sleep

from duckdb import DuckDBPyConnection

from .config import (
    GDAL_PARQUET_EXPORT_LCO,
    GDAL_SHP_LCO,
    PARQUET_OPTS,
    output_dir,
    output_file,
)
from .topology import check_gaps, check_overlaps
from .utils import parquet

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str, file: Path, layer: str) -> None:
    """Output results to file."""
    check_overlaps(conn, name, f"{name}_05")
    check_gaps(conn, name, f"{name}_05")

    # Materialize geometry joined with original attributes
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_06" AS
        SELECT a.geom AS geometry, b.* EXCLUDE (fid)
        FROM "{name}_05" AS a
        LEFT JOIN "{name}_attr" AS b
        ON a.fid = b.fid
    """)

    # Export to Parquet for GDAL
    p06 = parquet(f"{name}_06")
    conn.execute(f"COPY (SELECT * FROM \"{name}_06\") TO '{p06}' {PARQUET_OPTS}")

    shp_opts = GDAL_SHP_LCO if file.suffix == ".shp" else []
    parquet_opts = GDAL_PARQUET_EXPORT_LCO if file.suffix == ".parquet" else []
    dest = output_file or output_dir / file.name
    dest.parent.mkdir(exist_ok=True, parents=True)
    args = [
        *["gdal", "vector", "convert"],
        *[p06, dest],
        f"--output-layer={layer}",
        *shp_opts,
        *parquet_opts,
    ]
    try:
        success = False
        for retry in range(5):
            result = run(args, check=False, capture_output=True)
            if result.returncode == 0:
                success = True
                break
            sleep(retry**2)
        if not success:
            msg = f"could not write to output {name}"
            logger.error(msg)
            raise RuntimeError(msg)
    finally:
        Path(p06).unlink(missing_ok=True)
