import logging
from pathlib import Path
from subprocess import run
from time import sleep

import duckdb

from .config import (
    GDAL_PARQUET_LCO,
    GDAL_SHP_LCO,
    PARQUET_OPTS,
    output_dir,
    output_file,
)
from .topology import check_gaps, check_overlaps
from .utils import parquet

logger = logging.getLogger(__name__)


def main(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    file: Path,
    layer: str,
    *_: list,
) -> None:
    """Output results to file."""
    p05 = parquet(f"{name}_05")
    p06 = parquet(f"{name}_06")

    check_overlaps(conn, name, p05)
    check_gaps(conn, name, p05)

    # Materialize geometry joined with original attributes
    conn.execute(f"""--sql
        COPY (
            SELECT a.geometry, b.* EXCLUDE (fid)
            FROM read_parquet('{p05}') AS a
            LEFT JOIN read_parquet('{parquet(f"{name}_attr")}') AS b
            ON a.fid = b.fid
        ) TO '{p06}' {PARQUET_OPTS}
    """)

    shp_opts = GDAL_SHP_LCO if file.suffix == ".shp" else []
    parquet_opts = GDAL_PARQUET_LCO if file.suffix == ".parquet" else []
    dest = output_file or output_dir / file.name
    dest.parent.mkdir(exist_ok=True, parents=True)
    args = [
        *["gdal", "vector", "convert"],
        *[p06, dest],
        f"--output-layer={layer}",
        *shp_opts,
        *parquet_opts,
    ]
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
