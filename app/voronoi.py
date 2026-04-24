"""Generates Voronoi polygons from boundary points and clips to bounding extent."""

import duckdb

from .topology import check_gaps, check_missing_rows, check_overlaps
from .utils import coverage_clean


def main(conn: duckdb.DuckDBPyConnection, name: str, *_: list) -> None:
    """Create Voronoi polygons from points."""
    # Voronoi diagram from all input points
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp1" AS
        SELECT UNNEST(ST_Dump(
            ST_CollectionExtract(
                ST_VoronoiDiagram(ST_Collect(list(geometry))), 3
            )
        )).geom AS geometry
        FROM "{name}_03"
    """)

    # Assign source fid to each Voronoi cell via point-in-polygon.
    # ST_Intersects (not ST_Within) handles generators that land exactly on a
    # Voronoi cell boundary, which ST_Within would reject.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp2" AS
        SELECT a.fid, b.geometry
        FROM "{name}_03" AS a
        JOIN "{name}_04_tmp1" AS b
        ON ST_Intersects(a.geometry, b.geometry)
    """)
    check_missing_rows(conn, name, f"{name}_03", f"{name}_04_tmp2")

    # Union Voronoi cells by fid
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp3" AS
        SELECT fid, ST_Multi(ST_Union_Agg(geometry)) AS geometry
        FROM "{name}_04_tmp2"
        GROUP BY fid
    """)
    check_overlaps(conn, name, f"{name}_04_tmp3")

    # Coverage clean pass 1
    coverage_clean(conn, f"{name}_04_tmp3", f"{name}_04_tmp4")
    check_gaps(conn, name, f"{name}_04_tmp4")

    # Coverage clean pass 2
    coverage_clean(conn, f"{name}_04_tmp4", f"{name}_04")

    conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp1"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp2"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp3"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp4"')
