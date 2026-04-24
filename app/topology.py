"""Validates topology by checking for overlaps, gaps, and missing rows."""

import logging

import duckdb

logger = logging.getLogger(__name__)


def check_overlaps(conn: duckdb.DuckDBPyConnection, name: str, table: str) -> None:
    """Check for overlapping polygons."""
    overlaps = (
        conn.execute(f"""--sql
            SELECT EXISTS(
                SELECT 1
                FROM "{table}" AS a
                JOIN "{table}" AS b
                ON ST_Overlaps(a.geometry, b.geometry)
                WHERE a.fid != b.fid
            )
        """).fetchone()
        or [1]
    )[0]
    if overlaps:
        error = f"OVERLAPS: {name}"
        logger.error(error)
        raise RuntimeError(error)


def check_gaps(conn: duckdb.DuckDBPyConnection, name: str, table: str) -> None:
    """Check for gaps in polygon coverage."""
    gaps = (
        conn.execute(f"""--sql
            SELECT ST_NumInteriorRings(ST_Union_Agg(geometry))
            FROM "{table}"
        """).fetchone()
        or [0]
    )[0] > 0
    if gaps:
        error = f"GAPS: {name}"
        logger.error(error)
        raise RuntimeError(error)


def check_missing_rows(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    table_1: str,
    table_2: str,
) -> None:
    """Check that two tables have the same row count."""
    rows_1 = (conn.execute(f'SELECT count(*) FROM "{table_1}"').fetchone() or [0])[0]
    rows_2 = (conn.execute(f'SELECT count(*) FROM "{table_2}"').fetchone() or [0])[0]
    if rows_1 != rows_2:
        error = f"MISSING ROWS: {name}"
        logger.error(error)
        raise RuntimeError(error)
