"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection

from .config import SNAP_TOLERANCE, debug


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    # Extract Voronoi boundary lines that belong to the extension zone only.
    # ST_Difference removes every Voronoi edge that overlaps the _03a buffer
    # (buffered original endpoints), leaving only lines that delineate the area
    # beyond the original polygons. The NOT EXISTS filter drops any remaining
    # segments whose interior falls inside an original polygon (_01).
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

    # Snap _05_tmp1 endpoints that land within snap_dist of a _02b segment to the
    # exact closest point on that segment. GEOS intersection arithmetic in
    # ST_Difference (_05_tmp1) can drift ~1e-7° from original polygon vertices,
    # preventing ST_Node from creating a proper junction in _05. Per-segment bbox
    # pre-filter avoids collecting all of _02b into one large geometry, which
    # caused a ~7 GB peak when called with ST_ClosestPoint against the full
    # multi-geometry.
    snap_dist = SNAP_TOLERANCE * 2
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
        WITH
        ext AS (
            SELECT ROW_NUMBER() OVER () AS id, geom,
                ST_StartPoint(geom) AS start_pt,
                ST_EndPoint(geom) AS end_pt
            FROM "{name}_05_tmp1"
        ),
        start_bbox AS (
            SELECT e.id, e.start_pt AS pt, b.geom AS seg
            FROM ext e CROSS JOIN "{name}_02b" b
            WHERE ST_X(e.start_pt) BETWEEN ST_XMin(b.geom) - {snap_dist}
                                       AND ST_XMax(b.geom) + {snap_dist}
              AND ST_Y(e.start_pt) BETWEEN ST_YMin(b.geom) - {snap_dist}
                                       AND ST_YMax(b.geom) + {snap_dist}
        ),
        start_snap AS (
            SELECT id,
                MIN_BY(ST_ClosestPoint(seg, pt), ST_Distance(seg, pt)) AS snap_pt
            FROM start_bbox WHERE ST_Distance(pt, seg) < {snap_dist}
            GROUP BY id
        ),
        end_bbox AS (
            SELECT e.id, e.end_pt AS pt, b.geom AS seg
            FROM ext e CROSS JOIN "{name}_02b" b
            WHERE ST_X(e.end_pt) BETWEEN ST_XMin(b.geom) - {snap_dist}
                                     AND ST_XMax(b.geom) + {snap_dist}
              AND ST_Y(e.end_pt) BETWEEN ST_YMin(b.geom) - {snap_dist}
                                     AND ST_YMax(b.geom) + {snap_dist}
        ),
        end_snap AS (
            SELECT id,
                MIN_BY(ST_ClosestPoint(seg, pt), ST_Distance(seg, pt)) AS snap_pt
            FROM end_bbox WHERE ST_Distance(pt, seg) < {snap_dist}
            GROUP BY id
        ),
        pts_as_list AS (
            SELECT
                COALESCE(ss.snap_pt, e.start_pt) AS close_s,
                COALESCE(es.snap_pt, e.end_pt) AS close_e,
                list_transform(
                    generate_series(1, ST_NPoints(e.geom)),
                    lambda i: ST_PointN(e.geom, i::INTEGER)
                ) AS pts
            FROM ext e
            LEFT JOIN start_snap ss ON e.id = ss.id
            LEFT JOIN end_snap es ON e.id = es.id
        )
        SELECT ST_MakeLine(list_concat(
            [close_s],
            list_slice(pts, 2, -2),
            [close_e]
        )) AS geom
        FROM pts_as_list
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')

    # Split noding+polygonizing from the spatial join so DuckDB can release the
    # noding working memory before SPATIAL_JOIN begins. An RTREE index is added
    # after materialization since CTEs cannot be indexed.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp3" AS
        WITH
        lines AS (
            SELECT geom FROM "{name}_02b"
            UNION ALL
            SELECT geom FROM "{name}_05_tmp2"
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM lines
        )
        SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
        FROM noded
    """)
    conn.execute(
        f'CREATE INDEX "{name}_05_tmp3_rtree" ON "{name}_05_tmp3" USING RTREE (geom)'
    )

    # One representative interior point per polygon part (ST_Dump handles
    # multipolygons). Used by the SPATIAL_JOIN in _05 to assign each Voronoi cell
    # to the original polygon whose interior point falls inside it.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp4" AS
        WITH parts AS (
            SELECT * EXCLUDE (geom), UNNEST(ST_Dump(geom)).geom AS part_geom
            FROM "{name}_01"
        )
        SELECT * EXCLUDE (part_geom), ST_PointOnSurface(part_geom) AS pt
        FROM parts
    """)

    # LEFT JOIN so orphan extension cells are caught by the fallback rather than
    # silently dropped as gaps.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        WITH
        all_joined AS (
            SELECT c.geom AS vgeom, p.* EXCLUDE (pt)
            FROM "{name}_05_tmp3" AS c
            LEFT JOIN "{name}_05_tmp4" AS p ON ST_Within(p.pt, c.geom)
        ),
        unmatched AS (
            SELECT ROW_NUMBER() OVER () AS uid, vgeom
            FROM all_joined WHERE fid IS NULL
        ),
        fallback AS (
            SELECT u.vgeom, p.* EXCLUDE (pt)
            FROM unmatched u
            CROSS JOIN "{name}_05_tmp4" p
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
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp4"')
    conn.execute("CHECKPOINT")
