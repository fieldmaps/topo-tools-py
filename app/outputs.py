"""Validates topology and exports output files from the final merged geometry table."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from .config import COPY_OPTS, debug, output_dir, output_file
from .utils import has_coverage_violations, reassigned_fids

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


def _check_input_preserved(
    conn: DuckDBPyConnection, table_in: str, table_out: str
) -> None:
    """Fail if any input polygon isn't a subset of its same-fid output."""
    fids = reassigned_fids(conn, table_in, table_out)
    if not fids:
        return
    fids_csv = ",".join(str(f) for f in fids)
    bad = conn.execute(f"""--sql
        SELECT
            i.fid,
            ST_Area(i.geom) AS in_area,
            COALESCE(ST_Area(ST_Difference(i.geom, o.geom)),
                     ST_Area(i.geom)) AS lost_area,
            COALESCE(ST_Area(ST_Difference(i.geom, o.geom)),
                     ST_Area(i.geom)) / NULLIF(ST_Area(i.geom), 0) AS lost_pct
        FROM "{table_in}" i
        LEFT JOIN "{table_out}" o USING (fid)
        WHERE i.fid IN ({fids_csv})
        ORDER BY lost_pct DESC
    """).fetchall()
    details = ", ".join(
        f"fid={fid} ({100 * pct:.2f}% reassigned: lost={lost:.4g} of {in_a:.4g})"
        for fid, in_a, lost, pct in bad
    )
    error = f"INPUT NOT PRESERVED: {len(bad)} feature(s); worst: {details}"
    logger.error(error)
    raise RuntimeError(error)


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Output results to path."""
    _check_overlaps(conn, f"{name}_05")
    _check_gaps(conn, f"{name}_05")
    _check_input_preserved(conn, f"{name}_01", f"{name}_05")

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
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02b"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp4"')
