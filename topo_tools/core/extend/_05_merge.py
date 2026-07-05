"""Unions original polygons with Voronoi extensions, then coverage-cleans seams."""

from duckdb import DuckDBPyConnection

from ._constants import SNAP_TOLERANCE
from ._coverage import coverage_clean


def main(conn: DuckDBPyConnection, name: str, *, debug: bool = False) -> None:
    """Merge original geometry with Voronoi extensions, then coverage-clean seams."""
    # Per-part _01 with bbox cols. Parts (not whole multipolygon fids) keep the
    # bbox tight — a Chile fid can span mainland to a remote island, which would
    # make a whole-fid bbox match nearly everything.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp1" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_01"
        )
        SELECT fid, part_geom,
            ST_XMin(part_geom) AS xmin, ST_XMax(part_geom) AS xmax,
            ST_YMin(part_geom) AS ymin, ST_YMax(part_geom) AS ymax
        FROM parts
    """)

    # Original rows, UNION ALL each fid's non-empty extension remainder.
    # A single ST_Union_Agg(_01) as one global blob OOMs at Chile scale when
    # used as a per-fid ST_Difference operand (same failure mode as the
    # global-exterior line algebra ruled out in _02_lines.py). Instead,
    # bbox-prefiltered self-join per fid against nearby _01 parts only —
    # same pattern _02_lines.py already uses for the neighbor-union self-join.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
        WITH
        v AS (
            SELECT fid, geom,
                ST_XMin(geom) AS xmin, ST_XMax(geom) AS xmax,
                ST_YMin(geom) AS ymin, ST_YMax(geom) AS ymax
            FROM "{name}_04"
        ),
        neighbor_union AS (
            SELECT v.fid AS vfid, ST_Union_Agg(p.part_geom) AS geom
            FROM v
            JOIN "{name}_05_tmp1" p
              ON p.xmax >= v.xmin AND p.xmin <= v.xmax
             AND p.ymax >= v.ymin AND p.ymin <= v.ymax
            GROUP BY v.fid
        ),
        remainder AS (
            SELECT v.fid,
                ST_MakeValid(ST_CollectionExtract(
                    CASE WHEN n.geom IS NOT NULL
                        THEN ST_Difference(v.geom, n.geom)
                        ELSE v.geom
                    END, 3
                )) AS geom
            FROM v
            LEFT JOIN neighbor_union n ON v.fid = n.vfid
        )
        SELECT fid, geom FROM "{name}_01"
        UNION ALL
        SELECT fid, geom FROM remainder WHERE NOT ST_IsEmpty(geom)
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')

    # Dissolve to one row per fid, reattach original attribute columns.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp3" AS
        SELECT o.* EXCLUDE (geom), d.geom
        FROM (
            SELECT fid, ST_Union_Agg(geom) AS geom
            FROM "{name}_05_tmp2"
            GROUP BY fid
        ) d
        JOIN "{name}_01" o USING (fid)
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp2"')

    # Single whole-table coverage clean closes floating-point-scale seams left
    # by the independent per-fid ST_Difference calls above (GEOS recomputes
    # crossing points slightly differently each time — see docs/topology.md).
    # gap_max_width is tied to SNAP_TOLERANCE, not a sliver-vs-real-hole
    # heuristic: by construction every point of the extent belongs to exactly
    # one fid here, so there's no real feature left to protect from swallowing
    # — anything CoverageClean finds to close is seam noise, not a real gap.
    coverage_clean(conn, f"{name}_05_tmp3", f"{name}_05", None, SNAP_TOLERANCE)
