"""Validates topology and exports output files from the final merged geometry table."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from ._constants import COPY_OPTS
from ._coverage import has_coverage_violations

logger = getLogger(__name__)


def main(
    conn: DuckDBPyConnection, name: str, dest: Path, *, debug: bool = False
) -> None:
    """Output results to dest."""
    _check_overlaps(conn, f"{name}_05")
    _check_gaps(conn, f"{name}_05")

    dest.parent.mkdir(exist_ok=True, parents=True)

    conn.execute(f"""--sql
        COPY (
            SELECT * EXCLUDE (fid) RENAME (geom AS geometry)
            FROM "{name}_05"
        ) TO '{dest}' {COPY_OPTS[dest.suffix]}
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')


def _check_overlaps(conn: DuckDBPyConnection, table: str) -> None:
    if has_coverage_violations(conn, table):
        error = f"OVERLAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def _check_gaps(conn: DuckDBPyConnection, table: str) -> None:
    interior_rings = conn.execute(f"""--sql
        WITH u AS (
            SELECT ST_Union_Agg(geom) AS g
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
        )
        SELECT ST_NumInteriorRings(g)
        FROM u
    """).fetchall()[0][0]
    if (interior_rings or 0) > 0:
        error = f"GAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)
