"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection

from .config import debug
from .utils import spatial_join_memory


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    # Interior shared edges between adjacent original polygons. a.fid < b.fid avoids
    # duplicate pairs; DISTINCT drops any duplicate segments before noding;
    # ST_Intersects pre-filters before the more expensive ST_Intersection.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp1" AS
        SELECT DISTINCT
            UNNEST(ST_Dump(ST_LineMerge(line_geom))).geom AS geom
        FROM (
            SELECT
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
    # from tangent contacts before noding. Done per-row to avoid materializing
    # one large collected geometry for the entire Voronoi boundary set.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
        WITH orig_union AS (
            SELECT ST_Union_Agg(geom) AS geom FROM "{name}_01"
        )
        SELECT ST_CollectionExtract(ST_Difference(ST_Boundary(v.geom), o.geom), 2)
            AS geom
        FROM "{name}_04" AS v
        CROSS JOIN orig_union AS o
    """)

    # Node _05_tmp1 + _05_tmp2 and polygonize into final pieces. ST_Node
    # ensures every crossing is a shared vertex, eliminating topology seams.
    # ST_Difference in _05_tmp2 computes clip-point coordinates via GEOS
    # intersection arithmetic, which can drift from the exact vertex in _05_tmp1
    # by up to ~1e-7 degrees, preventing ST_Node from creating the required
    # junction node. Fix: decompose _05_tmp2 into 2-point segments and replace
    # any endpoint within 1e-7 of a _05_tmp1 vertex with the exact vertex
    # coordinate via ST_ClosestPoint. _05_tmp1 coordinates are never modified.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp3" AS
        WITH int_pts AS (
            SELECT ST_Collect(list(pt)) AS mpt
            FROM (
                SELECT UNNEST(ST_Dump(ST_Points(geom))).geom AS pt
                FROM "{name}_05_tmp1"
            )
        ),
        ext_lines AS (
            SELECT UNNEST(ST_Dump(geom)).geom AS geom
            FROM "{name}_05_tmp2"
            WHERE NOT ST_IsEmpty(geom)
        ),
        segments AS (
            SELECT
                ST_PointN(l.geom, t.i::INTEGER) AS p1,
                ST_PointN(l.geom, t.i::INTEGER + 1) AS p2
            FROM ext_lines l,
            UNNEST(generate_series(1, ST_NumPoints(l.geom) - 1)) AS t(i)
        ),
        snapped AS (
            SELECT
                CASE WHEN ST_Distance(s.p1, i.mpt) < 1e-7
                     THEN ST_ClosestPoint(i.mpt, s.p1)
                     ELSE s.p1
                END AS p1,
                CASE WHEN ST_Distance(s.p2, i.mpt) < 1e-7
                     THEN ST_ClosestPoint(i.mpt, s.p2)
                     ELSE s.p2
                END AS p2
            FROM segments s
            CROSS JOIN int_pts i
        ),
        lines AS (
            SELECT ST_MakeLine(p1, p2) AS geom
            FROM snapped
            WHERE NOT ST_Equals(p1, p2)
            UNION ALL
            SELECT geom FROM "{name}_05_tmp1"
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM lines
        )
        SELECT geom
        FROM (
            SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
            FROM noded
        )
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp2"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')

    # Each _05_tmp3 polygon is a full Voronoi cell (land + extension combined),
    # so each _01 centroid falls inside exactly one _05_tmp3 cell. All original
    # attributes are carried through so outputs.py requires no re-join.
    with spatial_join_memory(conn):
        conn.execute(
            f'CREATE INDEX "{name}_05_tmp3_ridx" ON "{name}_05_tmp3" USING RTREE (geom)'
        )
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_05" AS
            WITH pts AS (
                SELECT * EXCLUDE (geom), ST_PointOnSurface(geom) AS pt
                FROM "{name}_01"
            )
            SELECT p.* EXCLUDE (pt), ST_Multi(ST_Union_Agg(v.geom)) AS geom
            FROM "{name}_05_tmp3" AS v
            JOIN pts AS p ON ST_Within(p.pt, v.geom)
            GROUP BY ALL
        """)
        conn.execute(f'DROP INDEX "{name}_05_tmp3_ridx"')

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')
    conn.execute("CHECKPOINT")
