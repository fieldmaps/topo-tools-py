"""Generates Voronoi polygons from boundary points and clips to bounding extent."""

from duckdb import DuckDBPyConnection

from .config import debug


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Create Voronoi polygons from points."""
    # Voronoi diagram from all input points. The QUALIFY drops `_03b` rows that
    # are duplicates after rounding (x,y) to 1e-12 — a 0.1-picometer key that
    # only collapses true GEOS-precision duplicates while preserving every kept
    # point's original coordinate bit-for-bit. Without it, GEOS throws
    # "TopologyException: side location conflict" on cod_admin3, where adjacent
    # polygons sharing a boundary endpoint each interpolate a point at the same
    # location, producing exact-coordinate duplicate seeds that GEOS Voronoi
    # cannot disambiguate. 1e-12 is well above the GEOS noise floor — coarser
    # rounding (1e-14, 1e-15) doesn't dedup tightly enough to fix the conflict.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp1" AS
        WITH unique_pts AS (
            SELECT geom FROM "{name}_03b"
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY round(ST_X(geom), 12), round(ST_Y(geom), 12)
            ) = 1
        )
        SELECT UNNEST(ST_Dump(
            ST_CollectionExtract(
                ST_VoronoiDiagram(ST_Collect(list(geom))), 3
            )
        )).geom AS geom
        FROM unique_pts
    """)

    # Assign source fid to each Voronoi cell via point-in-polygon.
    # ST_Intersects (not ST_Within) handles generators that land exactly on a
    # Voronoi cell boundary, which ST_Within would reject.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp2" AS
        SELECT a.fid, b.geom
        FROM "{name}_03b" AS a
        JOIN "{name}_04_tmp1" AS b
        ON ST_Intersects(a.geom, b.geom)
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03b"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp1"')

    # Union Voronoi cells by fid
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04" AS
        SELECT fid, ST_Union_Agg(geom) AS geom
        FROM "{name}_04_tmp2"
        GROUP BY fid
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp2"')
