"""Validates topology by checking for overlaps, gaps, and missing rows."""

from logging import getLogger

from duckdb import DuckDBPyConnection

logger = getLogger(__name__)


def check_overlaps(conn: DuckDBPyConnection, name: str, table: str) -> None:
    """Check for overlapping polygons."""
    overlaps = conn.execute(f"""--sql
        SELECT ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL
        FROM "{table}"
    """).fetchall()[0][0]
    if overlaps:
        error = f"OVERLAPS: {name}"
        logger.error(error)
        raise RuntimeError(error)


def check_gaps(conn: DuckDBPyConnection, name: str, table: str) -> None:
    """Check for gaps in polygon coverage."""
    gaps = (
        conn.execute(f"""--sql
            SELECT ST_NumInteriorRings(ST_Union_Agg(geom))
            FROM "{table}"
        """).fetchall()[0][0]
        or 0
    ) > 0
    if gaps:
        error = f"GAPS: {name}"
        logger.error(error)
        raise RuntimeError(error)


def check_missing_rows(
    conn: DuckDBPyConnection, name: str, table_1: str, table_2: str
) -> None:
    """Check that two tables have the same row count."""
    rows_1 = conn.execute(f'SELECT count(*) FROM "{table_1}"').fetchall()[0][0] or 0
    rows_2 = conn.execute(f'SELECT count(*) FROM "{table_2}"').fetchall()[0][0] or 0
    if rows_1 != rows_2:
        error = f"MISSING ROWS: {name}"
        logger.error(error)
        raise RuntimeError(error)
