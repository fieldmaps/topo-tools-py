from pathlib import Path
from venv import logger

from psycopg import Connection
from psycopg.errors import InternalError_

from . import points, voronoi
from .config import distance, quiet


def main(conn: Connection, name: str, file: Path, layer: str, *_: list) -> None:
    """Try to generate voronoi polygons with multiple threshold values.

    First try running with default distance for points along line.
    If a memory error occurs, repeat by doubling distance values 10 times.
    Assuming the default start value of 0.0002, this sequence would be:
    0.0002, 0.0004, 0.0008, 0.0016, 0.0032, 0.0064, 0.0128, 0.0256, 0.0512, 0.1024.
    """
    for d in [distance * 2**i for i in range(10)]:
        try:
            points.main(conn, name, file, layer, d)
            voronoi.main(conn, name)
            if not quiet and d > distance:
                logger.info(f"done: {name}")
        except (RuntimeError, InternalError_) as e:
            if not quiet:
                logger.error(f"fail: {name} --distance={d}, {e}")
        else:
            return
    error = f"{name} did not succeed generating voronoi polygons"
    if not quiet:
        logger.error(error)
    raise RuntimeError(error)
