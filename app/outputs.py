from logging import getLogger
from pathlib import Path
from subprocess import run
from time import sleep

import duckdb

from .config import output_dir, output_file, quiet
from .topology import check_gaps, check_overlaps
from .utils import _PARQUET_OPTS, parquet

logger = getLogger(__name__)


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
    conn.execute(f"""
        COPY (
            SELECT a.geom, b.* EXCLUDE (fid)
            FROM read_parquet('{p05}') AS a
            LEFT JOIN read_parquet('{parquet(f"{name}_attr")}') AS b
            ON a.fid = b.fid
        ) TO '{p06}' {_PARQUET_OPTS}
    """)

    shp_opts = (
        ["--layer-creation-option=ENCODING=UTF-8"] if file.suffix == ".shp" else []
    )
    parquet_opts = (
        [
            "--layer-creation-option=COMPRESSION_LEVEL=15",
            "--layer-creation-option=COMPRESSION=ZSTD",
            "--layer-creation-option=GEOMETRY_NAME=geometry",
        ]
        if file.suffix == ".parquet"
        else []
    )
    dest = output_file or output_dir / file.name
    dest.parent.mkdir(exist_ok=True, parents=True)
    args = [
        *["gdal", "vector", "convert"],
        *[p06, dest],
        "--overwrite",
        "--quiet",
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
        if not quiet:
            logger.error("output fail: %s", name)
        msg = f"could not write to output {name}"
        raise RuntimeError(msg)
    if not quiet:
        logger.info("done: %s", name)
