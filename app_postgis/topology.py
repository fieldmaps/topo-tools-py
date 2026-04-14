from venv import logger

from psycopg import Connection
from psycopg.sql import SQL, Identifier

from .config import quiet


def check_overlaps(conn: Connection, name: str, table: str) -> None:
    """Check for overlaps in polygons."""
    query = """--sql
        SELECT EXISTS(
            SELECT 1
            FROM {table_in} a
            JOIN {table_in} b
            ON ST_Overlaps(a.geom, b.geom)
            WHERE a.fid != b.fid
        );
    """
    overlaps = (
        conn.execute(SQL(query).format(table_in=Identifier(table))).fetchone() or [1]
    )[0]
    if overlaps:
        error = f"OVERLAPS: {name}"
        if not quiet:
            logger.error(error)
        raise RuntimeError(error)


def check_gaps(conn: Connection, name: str, table: str) -> None:
    """Check for gaps in polygons."""
    query = """--sql
        SELECT ST_NumInteriorRings(ST_Union(geom))
        FROM {table_in};
    """
    gaps = (
        conn.execute(SQL(query).format(table_in=Identifier(table))).fetchone() or [0]
    )[0] > 0
    if gaps:
        error = f"GAPS: {name}"
        if not quiet:
            logger.error(error)
        raise RuntimeError(error)


def check_missing_rows(conn: Connection, name: str, table_1: str, table_2: str) -> None:
    """Check for missing rows in tables."""
    query = """--sql
        SELECT count(*)
        FROM {table_in};
    """
    rows_1 = (
        conn.execute(SQL(query).format(table_in=Identifier(table_1))).fetchone() or [0]
    )[0]
    rows_2 = (
        conn.execute(SQL(query).format(table_in=Identifier(table_2))).fetchone() or [0]
    )[0]
    if rows_1 != rows_2:
        error = f"MISSING ROWS: {name}"
        if not quiet:
            logger.error(error)
        raise RuntimeError(error)
