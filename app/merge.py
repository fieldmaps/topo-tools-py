"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection

from .config import SNAP_TOLERANCE, debug


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp1" AS
        WITH
        voronoi_lines AS (
            SELECT ST_Boundary(geom) AS geom FROM "{name}_04"
        ),
        cut_lines AS (
            SELECT geom FROM (
                SELECT ST_CollectionExtract(
                    ST_Difference(v.geom, c.geom), 2
                ) AS geom
                FROM voronoi_lines v CROSS JOIN "{name}_03a" c
            ) WHERE NOT ST_IsEmpty(geom)
        ),
        sections AS (
            SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM cut_lines
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
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
        WITH
        tmp1_collected AS MATERIALIZED (
            SELECT ST_Collect(list(geom)) AS geom FROM "{name}_02b"
        ),
        endpoints AS (
            SELECT
                ROW_NUMBER() OVER () AS id,
                t.geom,
                ST_NPoints(t.geom) AS npts,
                ST_ClosestPoint(c.geom, ST_StartPoint(t.geom)) AS close_s,
                ST_ClosestPoint(c.geom, ST_EndPoint(t.geom)) AS close_e
            FROM "{name}_05_tmp1" t CROSS JOIN tmp1_collected c
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
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')

    # LEFT JOIN instead of inner JOIN so that orphan cells (slivers produced by
    # noding that contain no original polygon centroid) are caught by the fallback
    # rather than silently dropped as gaps.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        WITH
        lines AS (
            SELECT geom FROM "{name}_02b"
            UNION ALL
            SELECT geom FROM "{name}_05_tmp2"
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
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02b"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp2"')
    conn.execute("CHECKPOINT")
