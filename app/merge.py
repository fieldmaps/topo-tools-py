"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection

from .utils import spatial_join_memory


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    # Interior shared edges between adjacent original polygons. Materialized
    # here (not in lines.py) so it does not sit on disk during points + voronoi.
    # a.fid < b.fid avoids duplicate pairs; ST_Intersects pre-filters before
    # the more expensive ST_Intersection.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05a_inner" AS
        SELECT
            fid_a,
            fid_b,
            UNNEST(ST_Dump(ST_LineMerge(line_geom))).geom AS geom
        FROM (
            SELECT
                a.fid AS fid_a,
                b.fid AS fid_b,
                ST_CollectionExtract(
                    ST_Intersection(ST_Boundary(a.geom), ST_Boundary(b.geom)), 2
                ) AS line_geom
            FROM "{name}_01" AS a
            JOIN "{name}_01" AS b
                ON a.fid < b.fid AND ST_Intersects(a.geom, b.geom)
        )
        WHERE NOT ST_IsEmpty(line_geom)
    """)

    # Clip Voronoi boundaries to the extension zone only. ST_Difference removes
    # every Voronoi edge inside the original coverage, leaving only the lines
    # that delineate the extension area. ST_CollectionExtract strips stray points
    # from tangent contacts before noding.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05b_ext" AS
        WITH orig_union AS (
            SELECT ST_Union_Agg(geom) AS geom FROM "{name}_01"
        ),
        voro_bd AS (
            SELECT ST_Collect(list(ST_Boundary(geom))) AS geom FROM "{name}_04"
        )
        SELECT ST_CollectionExtract(ST_Difference(v.geom, o.geom), 2) AS geom
        FROM voro_bd AS v, orig_union AS o
    """)

    # Node all three line sources together then polygonize into pieces.
    # _02 + _05a_inner exactly reconstruct every original polygon boundary, so
    # polygonize produces exactly one piece per original polygon — enabling the
    # one-to-one _05d_pip assignment below. ST_Node ensures every crossing is a
    # shared vertex, eliminating topology seams.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05c_pieces" AS
        WITH lines AS (
            SELECT geom FROM "{name}_05b_ext"
            UNION ALL
            SELECT geom FROM "{name}_02"
            UNION ALL
            SELECT geom FROM "{name}_05a_inner"
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM lines
        )
        SELECT row_number() OVER () AS pid, geom
        FROM (
            SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
            FROM noded
        )
    """)

    # Line sources have no further readers; release before the spatial joins.
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05a_inner"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05b_ext"')

    # One guaranteed-interior point per original polygon. Materialized here
    # (not in points.py) so it only exists during merge.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05d_pip" AS
        SELECT fid, ST_PointOnSurface(geom) AS geom
        FROM "{name}_01"
    """)

    with spatial_join_memory(conn):
        # Polygonize reconstructs each original polygon as exactly one piece,
        # so this is a one-to-one join. MIN(fid) is defensive for the
        # degenerate case of a pip landing exactly on a shared boundary.
        conn.execute(
            f'CREATE INDEX "{name}_05c_ridx" ON "{name}_05c_pieces" USING RTREE (geom)'
        )
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_05e_orig" AS
            SELECT p.pid, MIN(pip.fid) AS fid
            FROM "{name}_05d_pip" AS pip
            JOIN "{name}_05c_pieces" AS p ON ST_Within(pip.geom, p.geom)
            GROUP BY p.pid
        """)
        conn.execute(f'DROP INDEX "{name}_05c_ridx"')

    conn.execute(f'DROP TABLE IF EXISTS "{name}_05d_pip"')

    # Materialize interior points for extension pieces only — much smaller than
    # computing PointOnSurface for every piece in the dataset.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05f_pts" AS
        SELECT p.pid, ST_PointOnSurface(p.geom) AS pt
        FROM "{name}_05c_pieces" AS p
        WHERE p.pid NOT IN (SELECT pid FROM "{name}_05e_orig")
    """)

    with spatial_join_memory(conn):
        conn.execute(f'CREATE INDEX "{name}_04_ridx" ON "{name}_04" USING RTREE (geom)')
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_05g_voro" AS
            SELECT ep.pid, MIN(v.fid) AS fid
            FROM "{name}_05f_pts" AS ep
            JOIN "{name}_04" AS v ON ST_Within(ep.pt, v.geom)
            GROUP BY ep.pid
        """)
        conn.execute(f'DROP INDEX "{name}_04_ridx"')

    conn.execute(f'DROP TABLE IF EXISTS "{name}_05f_pts"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')

    # Fused assignment + final union: pieces JOIN orig/voro, group by fid in
    # one shot. Skips the _05_assigned materialization (same row count as
    # _05c_pieces, the largest table at this stage).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        SELECT COALESCE(orig.fid, voro.fid) AS fid,
               ST_Multi(ST_Union_Agg(p.geom)) AS geom
        FROM "{name}_05c_pieces" AS p
        LEFT JOIN "{name}_05e_orig" AS orig ON p.pid = orig.pid
        LEFT JOIN "{name}_05g_voro" AS voro ON p.pid = voro.pid
        WHERE COALESCE(orig.fid, voro.fid) IS NOT NULL
        GROUP BY 1
    """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_05e_orig"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05g_voro"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05c_pieces"')
    conn.execute("CHECKPOINT")
