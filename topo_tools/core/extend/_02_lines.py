"""Extracts polygon boundary lines and retains per-polygon attributes."""

from duckdb import DuckDBPyConnection


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Create boundary lines from polygons."""
    # Per-polygon boundary lines
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        SELECT fid, ST_Boundary(geom) AS geom
        FROM "{name}_01"
    """)

    # Per-polygon neighbor union, materialized via self-join with scalar bbox
    # predicates (no LATERAL, no ST_Intersects). Bbox-only is correct because a
    # non-touching neighbor adds nothing to ST_Difference / ST_Intersection
    # against a's boundary; the bbox prefilter is loose-but-safe. The join plans
    # as PIECEWISE_MERGE_JOIN, avoiding the SPATIAL_JOIN operator and its
    # ~1x RAM virtual reservation.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp2" AS
        SELECT a.fid AS afid, ST_Union_Agg(b.geom) AS neighbor_union
        FROM "{name}_02_tmp1" AS a
        JOIN "{name}_02_tmp1" AS b
          ON a.fid != b.fid
         AND ST_XMax(b.geom) >= ST_XMin(a.geom)
         AND ST_XMin(b.geom) <= ST_XMax(a.geom)
         AND ST_YMax(b.geom) >= ST_YMin(a.geom)
         AND ST_YMin(b.geom) <= ST_YMax(a.geom)
        GROUP BY a.fid
    """)

    # Exterior edges = each polygon's boundary minus its neighbour union.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT
            a.fid,
            UNNEST(ST_Dump(ST_LineMerge(ST_CollectionExtract(
                CASE WHEN n.neighbor_union IS NOT NULL
                    THEN ST_Difference(a.geom, n.neighbor_union)
                    ELSE a.geom
                END, 2
            )))).geom AS geom
        FROM "{name}_02_tmp1" AS a
        LEFT JOIN "{name}_02_tmp2" AS n ON a.fid = n.afid
    """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp1"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp2"')
