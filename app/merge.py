"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection

from .config import SNAP_TOLERANCE, debug


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    # Per-part _01 with precomputed bbox cols, reused by _05_tmp2 and _05.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp1" AS
        WITH parts AS (
            SELECT * EXCLUDE (geom), UNNEST(ST_Dump(geom)).geom AS part_geom
            FROM "{name}_01"
        )
        SELECT
            * EXCLUDE (part_geom),
            part_geom,
            ST_XMin(part_geom) AS xmin,
            ST_XMax(part_geom) AS xmax,
            ST_YMin(part_geom) AS ymin,
            ST_YMax(part_geom) AS ymax
        FROM parts
    """)

    # Drop only when BOTH endpoints are inside _01: midpoint test misclassed
    # arcs that briefly dip into _01, and a single-endpoint test is too
    # aggressive — rings can exit a void corner into _01 on a largely-void arc.
    # Bbox prefilter avoids SPATIAL_JOIN (~1x RAM reservation, OOMs); forces
    # HASH_JOIN + FILTER.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
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
            SELECT geom,
                ST_StartPoint(geom) AS start_pt,
                ST_EndPoint(geom) AS end_pt
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM cut_lines)
        )
        SELECT s.geom
        FROM sections s
        WHERE NOT EXISTS (
            SELECT 1 FROM "{name}_05_tmp1" p
            WHERE ST_X(s.start_pt) >= p.xmin
              AND ST_X(s.start_pt) <= p.xmax
              AND ST_Y(s.start_pt) >= p.ymin
              AND ST_Y(s.start_pt) <= p.ymax
              AND ST_Within(s.start_pt, p.part_geom)
        )
        OR NOT EXISTS (
            SELECT 1 FROM "{name}_05_tmp1" p
            WHERE ST_X(s.end_pt) >= p.xmin
              AND ST_X(s.end_pt) <= p.xmax
              AND ST_Y(s.end_pt) >= p.ymin
              AND ST_Y(s.end_pt) <= p.ymax
              AND ST_Within(s.end_pt, p.part_geom)
        )
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03a"')

    # Snap endpoints to discrete _02b corners. ST_Difference drifts ~1e-7°,
    # breaking ST_Node junctions; nearest-segment snap can overshoot and fuse
    # neighbours, discrete corners converge exactly.
    snap_dist = SNAP_TOLERANCE * 2
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp3" AS
        WITH
        corners AS (
            SELECT DISTINCT pt FROM (
                SELECT ST_StartPoint(geom) AS pt FROM "{name}_02b"
                UNION ALL
                SELECT ST_EndPoint(geom) FROM "{name}_02b"
            )
        ),
        ext AS (
            SELECT ROW_NUMBER() OVER () AS id, geom,
                ST_StartPoint(geom) AS start_pt,
                ST_EndPoint(geom) AS end_pt
            FROM "{name}_05_tmp2"
        ),
        start_snap AS (
            SELECT e.id,
                MIN_BY(c.pt, ST_Distance(c.pt, e.start_pt)) AS snap_pt
            FROM ext e CROSS JOIN corners c
            WHERE ST_X(c.pt) BETWEEN ST_X(e.start_pt) - {snap_dist}
                                 AND ST_X(e.start_pt) + {snap_dist}
              AND ST_Y(c.pt) BETWEEN ST_Y(e.start_pt) - {snap_dist}
                                 AND ST_Y(e.start_pt) + {snap_dist}
              AND ST_Distance(c.pt, e.start_pt) < {snap_dist}
            GROUP BY e.id
        ),
        end_snap AS (
            SELECT e.id,
                MIN_BY(c.pt, ST_Distance(c.pt, e.end_pt)) AS snap_pt
            FROM ext e CROSS JOIN corners c
            WHERE ST_X(c.pt) BETWEEN ST_X(e.end_pt) - {snap_dist}
                                 AND ST_X(e.end_pt) + {snap_dist}
              AND ST_Y(c.pt) BETWEEN ST_Y(e.end_pt) - {snap_dist}
                                 AND ST_Y(e.end_pt) + {snap_dist}
              AND ST_Distance(c.pt, e.end_pt) < {snap_dist}
            GROUP BY e.id
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
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp2"')

    # Separate from _05 so noding memory releases before the join.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp4" AS
        WITH
        lines AS (
            SELECT geom FROM "{name}_02b"
            UNION ALL
            SELECT geom FROM "{name}_05_tmp3"
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM lines
        )
        SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
        FROM noded
    """)
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02b"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')

    # Match each cell's interior point against _01 parts first, _04 as
    # fallback — routes concave/sliver sub-cells to the right fid.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        WITH
        cells AS (
            SELECT
                ROW_NUMBER() OVER () AS cid,
                geom AS vgeom,
                ST_PointOnSurface(geom) AS cpt
            FROM "{name}_05_tmp4"
        ),
        primary_match AS (
            SELECT c.cid, c.vgeom, c.cpt,
                   p.* EXCLUDE (part_geom, xmin, xmax, ymin, ymax)
            FROM cells c
            LEFT JOIN "{name}_05_tmp1" p
              ON ST_X(c.cpt) >= p.xmin
             AND ST_X(c.cpt) <= p.xmax
             AND ST_Y(c.cpt) >= p.ymin
             AND ST_Y(c.cpt) <= p.ymax
             AND ST_Within(c.cpt, p.part_geom)
        ),
        unmatched AS (
            SELECT cid, vgeom, cpt
            FROM primary_match WHERE fid IS NULL
        ),
        fallback AS (
            SELECT u.cid, u.vgeom, o.* EXCLUDE (geom)
            FROM unmatched u
            JOIN "{name}_04" v
              ON ST_X(u.cpt) >= v.xmin
             AND ST_X(u.cpt) <= v.xmax
             AND ST_Y(u.cpt) >= v.ymin
             AND ST_Y(u.cpt) <= v.ymax
             AND ST_Within(u.cpt, v.geom)
            JOIN "{name}_01" o ON o.fid = v.fid
        )
        SELECT * EXCLUDE (vgeom, cid), ST_Union_Agg(vgeom) AS geom
        FROM (
            SELECT * EXCLUDE (cpt) FROM primary_match WHERE fid IS NOT NULL
            UNION ALL
            SELECT * FROM fallback
        )
        GROUP BY ALL
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp4"')
