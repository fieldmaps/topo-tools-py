"""Generates Voronoi polygons from boundary points and clips to bounding extent."""

from duckdb import DuckDBPyConnection

from .config import debug


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Create Voronoi polygons from points."""
    # Voronoi diagram from all input points
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp1" AS
        SELECT UNNEST(ST_Dump(
            ST_CollectionExtract(
                ST_VoronoiDiagram(ST_Collect(list(geom))), 3
            )
        )).geom AS geom
        FROM "{name}_03b"
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

    # Union Voronoi cells by fid. ST_MakeValid defends against invalid cells
    # produced by ST_VoronoiDiagram on degenerate point configurations — feeding
    # an invalid polygon to ST_Union_Agg segfaults GEOS. Precomputed bbox columns
    # let downstream joins use plain numeric comparisons instead of recomputing
    # ST_XMin/etc per candidate pair.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04" AS
        WITH unioned AS (
            SELECT fid, ST_Union_Agg(ST_MakeValid(geom)) AS geom
            FROM "{name}_04_tmp2"
            GROUP BY fid
        )
        SELECT fid, geom,
            ST_XMin(geom) AS xmin,
            ST_XMax(geom) AS xmax,
            ST_YMin(geom) AS ymin,
            ST_YMax(geom) AS ymax
        FROM unioned
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp2"')
