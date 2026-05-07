"""Polygonizes cut lines and assigns each cell to its source fid."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from .utils import reassigned_fids

logger = getLogger(__name__)


def run(conn: DuckDBPyConnection, name: str) -> None:
    """Polygonize _02b + cut lines, assign fids, repair if any were lost."""
    _polygonize_and_assign(conn, name)
    _repair_if_needed(conn, name)


def _polygonize_and_assign(
    conn: DuckDBPyConnection, name: str, *, repair_clause: str = ""
) -> None:
    """Build _05_tmp4 (polygonize) then _05 (cell-to-fid join).

    `repair_clause` optionally appends extra constraint lines (e.g. `_02a`
    rings for fids that lost interior boundary in a prior pass).
    """
    # Separate from _05 so noding memory releases before the join.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp4" AS
        WITH
        lines AS (
            SELECT geom FROM "{name}_02b"
            UNION ALL
            SELECT geom FROM "{name}_05_tmp3"
            {repair_clause}
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM lines
        )
        SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
        FROM noded
    """)

    # Match each cell's interior point against _01 parts first, _04 as
    # fallback — routes concave/sliver sub-cells to the right fid.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        WITH
        cells AS (
            SELECT
                ROW_NUMBER() OVER () AS cid,
                geom AS vgeom,
                ST_PointOnSurface(geom) AS cpt
            FROM "{name}_05_tmp4"
        ),
        primary_match AS (
            SELECT c.cid, c.vgeom, c.cpt,
                   p.* EXCLUDE (part_geom, xmin, xmax, ymin, ymax)
            FROM cells c
            LEFT JOIN "{name}_05_tmp1" p
              ON ST_X(c.cpt) >= p.xmin
             AND ST_X(c.cpt) <= p.xmax
             AND ST_Y(c.cpt) >= p.ymin
             AND ST_Y(c.cpt) <= p.ymax
             AND ST_Within(c.cpt, p.part_geom)
        ),
        unmatched AS (
            SELECT cid, vgeom, cpt
            FROM primary_match WHERE fid IS NULL
        ),
        fallback AS (
            SELECT u.cid, u.vgeom, o.* EXCLUDE (geom)
            FROM unmatched u
            JOIN "{name}_04" v
              ON ST_X(u.cpt) >= v.xmin
             AND ST_X(u.cpt) <= v.xmax
             AND ST_Y(u.cpt) >= v.ymin
             AND ST_Y(u.cpt) <= v.ymax
             AND ST_Within(u.cpt, v.geom)
            JOIN "{name}_01" o ON o.fid = v.fid
        )
        SELECT * EXCLUDE (vgeom, cid), ST_Union_Agg(vgeom) AS geom
        FROM (
            SELECT * EXCLUDE (cpt) FROM primary_match WHERE fid IS NOT NULL
            UNION ALL
            SELECT * FROM fallback
        )
        GROUP BY ALL
    """)


def _repair_if_needed(conn: DuckDBPyConnection, name: str) -> None:
    """Surgical → global escalation when fids lost interior-boundary area.

    Mirrors `clean.py`: try the targeted fix first, fall back to global only
    if it fails. Global pass will OOM at Chile-coastline scale; that's the
    same failure surface as today's INPUT NOT PRESERVED on unfixable inputs.
    """
    fids = reassigned_fids(conn, f"{name}_01", f"{name}_05")
    if not fids:
        return
    logger.info("repair surgical: %d fid(s) reassigned, injecting _02a", len(fids))
    fids_csv = ",".join(str(f) for f in fids)
    surgical = f'UNION ALL SELECT geom FROM "{name}_02a" WHERE fid IN ({fids_csv})'
    _polygonize_and_assign(conn, name, repair_clause=surgical)
    fids = reassigned_fids(conn, f"{name}_01", f"{name}_05")
    if not fids:
        return
    logger.info("repair global: %d fid(s) still reassigned, escalating", len(fids))
    global_clause = f'UNION ALL SELECT geom FROM "{name}_02a"'
    _polygonize_and_assign(conn, name, repair_clause=global_clause)
