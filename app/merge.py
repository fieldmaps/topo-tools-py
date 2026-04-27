"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection

from .config import SNAP_TOLERANCE, debug


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

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
        WITH
        voronoi_lines AS (
            SELECT ST_Boundary(geom) AS geom FROM "{name}_04"
        ),
        cut_geom AS (
            SELECT ST_Buffer(ST_Collect(list(geom)), {SNAP_TOLERANCE}) AS geom
            FROM "{name}_02a"
        ),
        cut_lines AS (
            SELECT geom FROM (
                SELECT ST_CollectionExtract(
                    ST_Difference(v.geom, c.geom), 2
                ) AS geom
                FROM voronoi_lines v CROSS JOIN cut_geom c
            ) WHERE NOT ST_IsEmpty(geom)
        ),
        unioned AS (
            SELECT ST_LineMerge(ST_Union_Agg(geom)) AS geom FROM cut_lines
        ),
        sections AS (
            SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM unioned
        )
        SELECT s.geom
        FROM sections s
        WHERE NOT EXISTS (
            SELECT 1 FROM "{name}_01" p
            WHERE ST_Within(ST_PointOnSurface(s.geom), p.geom)
        )
    """)

    snap_dist = SNAP_TOLERANCE * 2
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp3" AS
        WITH
        tmp1_collected AS MATERIALIZED (
            SELECT ST_Collect(list(geom)) AS geom FROM "{name}_05_tmp1"
        ),
        endpoints AS (
            SELECT
                ROW_NUMBER() OVER () AS id,
                t.geom,
                ST_NPoints(t.geom) AS npts,
                ST_ClosestPoint(c.geom, ST_StartPoint(t.geom)) AS close_s,
                ST_ClosestPoint(c.geom, ST_EndPoint(t.geom)) AS close_e
            FROM "{name}_05_tmp2" t CROSS JOIN tmp1_collected c
        ),
        pts AS (
            SELECT id, npts, close_s, close_e, geom,
                unnest(generate_series(1, npts)) AS idx
            FROM endpoints
        ),
        rebuilt AS (
            SELECT id, ST_MakeLine(list(
                CASE
                    WHEN idx = 1
                        AND ST_Distance(ST_StartPoint(geom), close_s) < {snap_dist}
                        THEN close_s
                    WHEN idx = npts
                        AND ST_Distance(ST_EndPoint(geom), close_e) < {snap_dist}
                        THEN close_e
                    ELSE ST_PointN(geom, idx::INTEGER)
                END
                ORDER BY idx
            )) AS geom
            FROM pts
            GROUP BY id
        )
        SELECT geom FROM rebuilt
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp2"')

    # Inline the former _05_tmp3 polygonization and spatial assignment together.
    # LEFT JOIN instead of inner JOIN so that orphan cells (slivers produced by
    # noding that contain no original polygon centroid) are caught by the fallback
    # rather than silently dropped as gaps.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        WITH
        lines AS (
            SELECT geom FROM "{name}_05_tmp1"
            UNION ALL
            SELECT geom FROM "{name}_05_tmp3"
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM lines
        ),
        cells AS (
            SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
            FROM noded
        ),
        parts AS (
            SELECT * EXCLUDE (geom), UNNEST(ST_Dump(geom)).geom AS part_geom
            FROM "{name}_01"
        ),
        pts AS (
            SELECT * EXCLUDE (part_geom), ST_PointOnSurface(part_geom) AS pt
            FROM parts
        ),
        all_joined AS (
            SELECT c.geom AS vgeom, p.* EXCLUDE (pt)
            FROM cells AS c
            LEFT JOIN pts AS p ON ST_Within(p.pt, c.geom)
        ),
        unmatched AS (
            SELECT ROW_NUMBER() OVER () AS uid, vgeom
            FROM all_joined WHERE fid IS NULL
        ),
        fallback AS (
            SELECT u.vgeom, p.* EXCLUDE (pt)
            FROM unmatched u
            CROSS JOIN pts p
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY u.uid
                ORDER BY ST_Distance(ST_Centroid(u.vgeom), p.pt)
            ) = 1
        )
        SELECT * EXCLUDE (vgeom), ST_Collect(list(vgeom)) AS geom
        FROM (
            SELECT * FROM all_joined WHERE fid IS NOT NULL
            UNION ALL
            SELECT * FROM fallback
        )
        GROUP BY ALL
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')
    conn.execute("CHECKPOINT")
