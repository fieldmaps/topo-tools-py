from pathlib import Path
from subprocess import run
from time import sleep
from typing import LiteralString
from venv import logger

from psycopg import Connection
from psycopg.sql import SQL, Identifier

from .config import dbname, output_dir, output_file, quiet
from .topology import check_gaps, check_overlaps

query_1: LiteralString = """--sql
    DROP VIEW IF EXISTS {table_out};
    CREATE VIEW {table_out} AS
    SELECT
        a.geom,
        b.*
    FROM {table_in1} AS a
    LEFT JOIN {table_in2} AS b
    ON a.fid = b.fid;
"""


def main(conn: Connection, name: str, file: Path, layer: str, *_: list) -> None:
    """Output results to file."""
    check_overlaps(conn, name, f"{name}_05")
    check_gaps(conn, name, f"{name}_05")
    conn.execute(
        SQL(query_1).format(
            table_in1=Identifier(f"{name}_05"),
            table_in2=Identifier(f"{name}_attr"),
            table_out=Identifier(f"{name}_06"),
        ),
    )
    shp = ["--layer-creation-option=ENCODING=UTF-8"] if file.suffix == ".shp" else []
    parquet = (
        [
            "--layer-creation-option=COMPRESSION_LEVEL=15",
            "--layer-creation-option=COMPRESSION=ZSTD",
            "--layer-creation-option=GEOMETRY_NAME=geometry",
        ]
        if file.suffix == ".parquet"
        else []
    )
    output_path = output_file or output_dir / file.name
    output_path.parent.mkdir(exist_ok=True, parents=True)
    args = [
        *["gdal", "vector", "convert"],
        *[f"PG:dbname={dbname}", output_path],
        "--overwrite",
        "--quiet",
        f"--input-layer={name}_06",
        f"--output-layer={layer}",
        *shp,
        *parquet,
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
            logger.error(f"output fail: {name}")
        error = f"could not write to output {name}"
        raise RuntimeError(error)
    if not quiet:
        logger.info(f"done: {name}")
