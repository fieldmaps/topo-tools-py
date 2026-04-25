"""Unions Voronoi extensions with original polygons and cleans topology."""

from duckdb import DuckDBPyConnection

from .utils import coverage_clean


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    # Voronoi extension clipped to outside original coverage.
    # Per-cell lateral union avoids a global ST_Union_Agg over all original
    # polygons, which OOMs for large/complex datasets.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp1" AS
        SELECT
            a.fid,
            ST_Multi(ST_MakeValid(
                ST_Difference(a.geom, b.local_union)
            )) AS geom
        FROM "{name}_04" AS a
        CROSS JOIN LATERAL (
            SELECT ST_Union_Agg(c.geom) AS local_union
            FROM "{name}_01" AS c
            WHERE ST_Intersects(a.geom, c.geom)
        ) AS b
        WHERE b.local_union IS NOT NULL
    """)

    # Original polygons plus the Voronoi extension outside the original coverage
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
        SELECT fid, geom FROM "{name}_01"
        UNION ALL
        SELECT fid, geom FROM "{name}_05_tmp1"
    """)

    # Re-union by fid to merge original + extended parts
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp3" AS
        SELECT fid, ST_Multi(ST_Union_Agg(geom)) AS geom
        FROM "{name}_05_tmp2"
        GROUP BY fid
    """)

    # Coverage clean
    coverage_clean(conn, f"{name}_05_tmp3", f"{name}_05")

    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp2"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')
