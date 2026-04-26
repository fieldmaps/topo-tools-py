"""Validates topology by checking for overlaps, gaps, and missing rows."""

from logging import getLogger

from duckdb import DuckDBPyConnection

logger = getLogger(__name__)


def check_overlaps(conn: DuckDBPyConnection, table: str) -> None:
    """Check for overlapping polygons using ST_CoverageInvalidEdges_Agg."""
    has_overlaps = conn.execute(f"""--sql
        SELECT ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL
        FROM "{table}"
    """).fetchall()[0][0]

    if has_overlaps:
        error = f"OVERLAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def check_gaps(conn: DuckDBPyConnection, table: str) -> None:
    """Check for gaps in polygon coverage using interior ring count."""
    interior_rings = conn.execute(f"""--sql
        WITH u AS (SELECT ST_Union_Agg(geom) AS g FROM "{table}")
        SELECT ST_NumInteriorRings(g)
        FROM u
    """).fetchall()[0][0]
    has_gaps = (interior_rings or 0) > 0

    if has_gaps:
        error = f"GAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def check_missing_rows(conn: DuckDBPyConnection, table_1: str, table_2: str) -> None:
    """Check that two tables have the same row count."""
    rows_1 = conn.execute(f'SELECT count(*) FROM "{table_1}"').fetchall()[0][0] or 0
    rows_2 = conn.execute(f'SELECT count(*) FROM "{table_2}"').fetchall()[0][0] or 0
    if rows_1 != rows_2:
        error = (
            f"MISSING ROWS: {table_1} has {rows_1} rows, "
            f"but {table_2} has {rows_2} rows"
        )
        logger.error(error)
        raise RuntimeError(error)
