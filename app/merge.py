"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection

from .utils import spatial_join_memory


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    # Node ALL boundaries together then polygonize into pieces. ST_Node ensures
    # every crossing point is a shared vertex in both geometries, which guarantees
    # adjacent pieces share exact boundary coordinates — no topology seams.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_pieces" AS
        WITH orig_bd AS (
            SELECT ST_Union_Agg(ST_Boundary(geom)) AS geom FROM "{name}_01"
        ),
        voro_bd AS (
            SELECT ST_Union_Agg(ST_Boundary(geom)) AS geom FROM "{name}_04"
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM (
                SELECT geom FROM orig_bd
                UNION ALL
                SELECT geom FROM voro_bd
            )
        )
        SELECT row_number() OVER () AS pid, geom
        FROM (
            SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
            FROM noded
        )
    """)

    # Point-on-surface WITHOUT geometry — keeps this table small so the
    # spatial joins don't fill the buffer pool.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_pts" AS
        SELECT pid, ST_PointOnSurface(geom) AS pt
        FROM "{name}_05_pieces"
    """)

    with spatial_join_memory(conn):
        # Assign pieces inside original polygons. MIN(fid) deduplicates the rare
        # case where a centroid lands exactly on a shared boundary and ST_Within
        # matches two adjacent polygons.
        conn.execute(f'CREATE INDEX "{name}_01_ridx" ON "{name}_01" USING RTREE (geom)')
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_05_orig" AS
            SELECT p.pid, MIN(o.fid) AS fid
            FROM "{name}_05_pts" AS p
            JOIN "{name}_01" AS o ON ST_Within(p.pt, o.geom)
            GROUP BY p.pid
        """)
        conn.execute(f'DROP INDEX "{name}_01_ridx"')

    with spatial_join_memory(conn):
        # Assign remaining pieces (extension areas) to Voronoi cells. Every
        # extension piece is within exactly one Voronoi cell — no fallback needed.
        conn.execute(f'CREATE INDEX "{name}_04_ridx" ON "{name}_04" USING RTREE (geom)')
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_05_voro" AS
            SELECT p.pid, MIN(v.fid) AS fid
            FROM "{name}_05_pts" AS p
            JOIN "{name}_04" AS v ON ST_Within(p.pt, v.geom)
            WHERE p.pid NOT IN (SELECT pid FROM "{name}_05_orig")
            GROUP BY p.pid
        """)
        conn.execute(f'DROP INDEX "{name}_04_ridx"')

    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_pts"')

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_assigned" AS
        SELECT COALESCE(orig.fid, voro.fid) AS fid, p.geom
        FROM "{name}_05_pieces" AS p
        LEFT JOIN "{name}_05_orig" AS orig ON p.pid = orig.pid
        LEFT JOIN "{name}_05_voro" AS voro ON p.pid = voro.pid
        WHERE COALESCE(orig.fid, voro.fid) IS NOT NULL
    """)
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_orig"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_voro"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_pieces"')

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        SELECT fid, ST_Multi(ST_Union_Agg(geom)) AS geom
        FROM "{name}_05_assigned"
        GROUP BY fid
    """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_assigned"')
