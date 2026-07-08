"""Computes pairwise overlap ratios between the old and new layers."""

from duckdb import DuckDBPyConnection

from ._constants import EQUAL_AREA_CRS, INTERSECTION_SLIVER_DEG2


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Compute shared_area/coverage_a/coverage_b/iou for every touching (a, b) pair.

    Bbox-prefiltered join on ST_Dump-exploded parts of both layers -- never a
    bare spatial predicate in the JOIN condition, which would trigger DuckDB's
    SPATIAL_JOIN operator and its ~1x-RAM virtual reservation (see
    docs/topology.md) -- same pattern as core/match/_02_assign.py. Unlike that
    assign stage (top-1 parent per child), every pair with shared_area > 0 is
    kept: classification needs the full pair graph, not just the best match
    per fid. Native DuckDB always uses exact ST_Intersection here -- no
    point-sampling fallback (see docs/change.md for why the JS version's
    WASM-only fallback doesn't apply).
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_a_01"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp2" AS
        SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_b_01"
    """)

    # Sliver crumbs are dropped by raw (untransformed) degree^2 area, matching
    # topo-tools-js's overlap.ts -- cheap pre-filter before the equal-area
    # transform, which is only ever applied to surviving intersection geometry.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp3" AS
        WITH crumbs AS (
            SELECT a.fid AS a_fid, b.fid AS b_fid,
                   ST_CollectionExtract(
                       ST_Intersection(a.part_geom, b.part_geom), 3
                   ) AS geom
            FROM "{name}_02_tmp1" a
            JOIN "{name}_02_tmp2" b
              ON ST_XMax(b.part_geom) >= ST_XMin(a.part_geom)
             AND ST_XMin(b.part_geom) <= ST_XMax(a.part_geom)
             AND ST_YMax(b.part_geom) >= ST_YMin(a.part_geom)
             AND ST_YMin(b.part_geom) <= ST_YMax(a.part_geom)
             AND ST_Intersects(a.part_geom, b.part_geom)
        ),
        filtered AS (
            SELECT a_fid, b_fid, geom FROM crumbs
            WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
              AND ST_Area(geom) >= {INTERSECTION_SLIVER_DEG2}
        )
        SELECT a_fid, b_fid,
               SUM(
                   ST_Area(ST_Transform(geom, 'EPSG:4326', '{EQUAL_AREA_CRS}'))
               ) AS shared_area
        FROM filtered
        GROUP BY a_fid, b_fid
    """)

    # Whole-geometry area per fid, computed once (not once per pair).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp4" AS
        SELECT fid, ST_Area(ST_Transform(geom, 'EPSG:4326', '{EQUAL_AREA_CRS}')) AS area
        FROM "{name}_a_01"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp5" AS
        SELECT fid, ST_Area(ST_Transform(geom, 'EPSG:4326', '{EQUAL_AREA_CRS}')) AS area
        FROM "{name}_b_01"
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT p.a_fid, p.b_fid, p.shared_area,
               p.shared_area / NULLIF(aa.area, 0) AS coverage_a,
               p.shared_area / NULLIF(ba.area, 0) AS coverage_b,
               p.shared_area / NULLIF(aa.area + ba.area - p.shared_area, 0) AS iou
        FROM "{name}_02_tmp3" p
        JOIN "{name}_02_tmp4" aa ON aa.fid = p.a_fid
        JOIN "{name}_02_tmp5" ba ON ba.fid = p.b_fid
    """)

    for n in range(1, 6):
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp{n}"')
