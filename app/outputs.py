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


def _check_missing_area(
    conn: DuckDBPyConnection, table_in: str, table_out: str
) -> None:
    """Fail if any output feature shrank below its input area.

    Extension only adds area, so any shrinkage means polygonize fused part
    of the feature with a neighbour. LEFT JOIN treats missing fids as area
    0 so they're flagged too. 0.1% tolerance for GEOS noise.
    """
    bad = conn.execute(f"""--sql
        WITH ins AS (SELECT fid, ST_Area(geom) AS area FROM "{table_in}"),
             outs AS (SELECT fid, ST_Area(geom) AS area FROM "{table_out}")
        SELECT i.fid, i.area, COALESCE(o.area, 0) AS out_area,
               (i.area - COALESCE(o.area, 0)) / NULLIF(i.area, 0) AS lost_pct
        FROM ins i LEFT JOIN outs o USING (fid)
        WHERE COALESCE(o.area, 0) < i.area * 0.999
        ORDER BY lost_pct DESC
        LIMIT 5
    """).fetchall()
    if bad:
        details = ", ".join(
            f"fid={fid} ({100 * pct:.1f}% lost: {in_a:.4f}->{out_a:.4f})"
            for fid, in_a, out_a, pct in bad
        )
        error = f"MISSING AREA: {len(bad)} feature(s); worst: {details}"
        logger.error(error)
        raise RuntimeError(error)


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Output results to path."""
    for run_check in [
        lambda: _check_overlaps(conn, f"{name}_05"),
        lambda: _check_gaps(conn, f"{name}_05"),
        lambda: _check_missing_area(conn, f"{name}_01", f"{name}_05"),
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
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
