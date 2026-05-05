"""Retries points + voronoi stages with doubling distance on failure."""

from logging import getLogger

from duckdb import DuckDBPyConnection
from duckdb import Error as DuckDBError

from . import points, voronoi
from .config import MAX_POINTS, distance

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Try to generate Voronoi polygons with multiple distance thresholds.

    First try running with the default distance for points along a line.
    If an error occurs, repeat by doubling the distance up to 10 times.
    Assuming the default start value of 0.0002, this sequence would be:
    0.0002, 0.0004, 0.0008, 0.0016, 0.0032, 0.0064, 0.0128, 0.0256, 0.0512, 0.1024.
    """
    for d in [distance * 2**i for i in range(10)]:
        try:
            points.main(conn, name, d)
            count = conn.execute(f'SELECT count(*) FROM "{name}_03b"').fetchall()[0][0]
            _check_point_count(count)
            voronoi.main(conn, name)
        except (RuntimeError, DuckDBError) as e:
            logger.warning("fail: %s --distance=%s: %s", name, d, e)
        else:
            return
    error = f"{name} did not succeed generating voronoi polygons"
    logger.error(error)
    raise RuntimeError(error)


def _check_point_count(count: int) -> None:
    if count > MAX_POINTS:
        msg = f"too many points: {count:,}"
        raise RuntimeError(msg)
