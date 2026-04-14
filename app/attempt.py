import logging
from pathlib import Path

import duckdb

from . import points, voronoi
from .config import distance
from .utils import cleanup_tmp

logger = logging.getLogger(__name__)


def main(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    file: Path,
    layer: str,
    *_: list,
) -> None:
    """Try to generate Voronoi polygons with multiple distance thresholds.

    First try running with the default distance for points along a line.
    If an error occurs, repeat by doubling the distance up to 10 times.
    Assuming the default start value of 0.0002, this sequence would be:
    0.0002, 0.0004, 0.0008, 0.0016, 0.0032, 0.0064, 0.0128, 0.0256, 0.0512, 0.1024.
    """
    for d in [distance * 2**i for i in range(10)]:
        try:
            points.main(conn, name, file, layer, d)
            voronoi.main(conn, name)
        except (RuntimeError, duckdb.Error) as e:
            logger.warning("fail: %s --distance=%s: %s", name, d, e)
            cleanup_tmp(name)
        else:
            return
    error = f"{name} did not succeed generating voronoi polygons"
    logger.error(error)
    raise RuntimeError(error)
