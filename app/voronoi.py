"""Generates Voronoi polygons from boundary points and clips to bounding extent."""

from duckdb import DuckDBPyConnection

from .topology import check_gaps, check_missing_rows, check_overlaps


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
        FROM "{name}_03"
    """)

    # Assign source fid to each Voronoi cell via point-in-polygon.
    # ST_Intersects (not ST_Within) handles generators that land exactly on a
    # Voronoi cell boundary, which ST_Within would reject.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp2" AS
        SELECT a.fid, b.geom
        FROM "{name}_03" AS a
        JOIN "{name}_04_tmp1" AS b
        ON ST_Intersects(a.geom, b.geom)
    """)
    check_missing_rows(conn, f"{name}_03", f"{name}_04_tmp2")

    # Union Voronoi cells by fid
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp3" AS
        SELECT fid, ST_Union_Agg(geom) AS geom
        FROM "{name}_04_tmp2"
        GROUP BY fid
    """)
    check_overlaps(conn, f"{name}_04_tmp3")

    check_gaps(conn, f"{name}_04_tmp3")

    conn.execute(f'ALTER TABLE "{name}_04_tmp3" RENAME TO "{name}_04"')

    conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp1"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp2"')
