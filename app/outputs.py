"""Validates topology and exports output files from the final merged geometry table."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from .config import COPY_OPTS, debug, output_dir, output_file
from .utils import has_coverage_violations

logger = getLogger(__name__)


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


def _check_missing_rows(conn: DuckDBPyConnection, table_1: str, table_2: str) -> None:
    rows_1 = conn.execute(f'SELECT count(*) FROM "{table_1}"').fetchall()[0][0] or 0
    rows_2 = conn.execute(f'SELECT count(*) FROM "{table_2}"').fetchall()[0][0] or 0
    if rows_1 != rows_2:
        error = (
            f"MISSING ROWS: {table_1} has {rows_1} rows, "
            f"but {table_2} has {rows_2} rows"
        )
        logger.error(error)
        raise RuntimeError(error)


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Output results to path."""
    for run_check in [
        lambda: _check_overlaps(conn, f"{name}_05"),
        lambda: _check_gaps(conn, f"{name}_05"),
        lambda: _check_missing_rows(conn, f"{name}_05", f"{name}_01"),
    ]:
        try:
            run_check()
        except RuntimeError as e:
            logger.warning(e)

    dest = output_file or output_dir / path.name
    dest.parent.mkdir(exist_ok=True, parents=True)

    conn.execute(f"""--sql
        COPY (
            SELECT * EXCLUDE (fid) RENAME (geom AS geometry)
            FROM "{name}_05"
        ) TO '{dest}' {COPY_OPTS[path.suffix]}
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
