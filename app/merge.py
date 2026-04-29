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
    #
    # The bbox prefilter on the NOT EXISTS subquery is required: without it,
    # DuckDB rewrites the correlated ST_Within into a SPATIAL_JOIN, which
    # pre-allocates ~1x RAM as a virtual spill reservation and triggers OOMs on
    # constrained memory budgets. With explicit ST_X/ST_Y vs ST_XMin/XMax/
    # YMin/YMax comparisons, the planner uses HASH_JOIN + FILTER instead.
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
        sections_pt AS (
            SELECT geom, ST_PointOnSurface(geom) AS pt
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM cut_lines)
        )
        SELECT s.geom
        FROM sections_pt s
        WHERE NOT EXISTS (
            SELECT 1 FROM "{name}_01" p
            WHERE ST_X(s.pt) >= ST_XMin(p.geom)
              AND ST_X(s.pt) <= ST_XMax(p.geom)
              AND ST_Y(s.pt) >= ST_YMin(p.geom)
              AND ST_Y(s.pt) <= ST_YMax(p.geom)
              AND ST_Within(s.pt, p.geom)
        )
    """)

    # Snap _05_tmp1 endpoints that land within snap_dist of a _02b polyline
    # endpoint (a 3+ polygon corner) to the exact corner. GEOS intersection
    # arithmetic in ST_Difference (_05_tmp1) can drift ~1e-7° from original
    # polygon vertices, preventing ST_Node from creating a proper junction in
    # _05. Snapping to the nearest segment via ST_ClosestPoint can land just
    # past a corner (when the perpendicular projection falls on the next
    # segment of the merged polyline), leaving a sub-nanodegree gap that fuses
    # neighbouring polygons in ST_Polygonize. Snapping to a discrete corner
    # set guarantees convergence at the exact corner coordinates.
    snap_dist = SNAP_TOLERANCE * 2
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp2" AS
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
            FROM "{name}_05_tmp1"
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
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp1"')

    # Split noding+polygonizing from the spatial join so DuckDB can release the
    # noding working memory before SPATIAL_JOIN begins.
    #
    # Polygonize input is `_02b` (interior shared edges) + `_05_tmp2`
    # (extension boundary), plus `_02a` (per-fid exterior edges) ONLY for
    # polygons clean.py modified. Clean's polygonize-and-reattribute pass adds
    # crossing vertices to a modified polygon's boundary that are NOT present
    # in the points used to compute `_05_tmp2`'s extension boundary, leaving
    # sub-pixel gaps where the polygon meets the extension area; `_02a` for
    # the affected polygons closes the cell from the polygon's own boundary.
    # For unmodified polygons (entire dataset on clean inputs like Chile),
    # `_02b` + `_05_tmp2` already closes everything — adding all of `_02a`
    # there would near-duplicate `_05_tmp2` along long coastlines and explode
    # cell count via thin sliver atoms, OOM'ing the SPATIAL_JOIN.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_tmp3" AS
        WITH
        lines AS (
            SELECT geom FROM "{name}_02b"
            UNION ALL
            SELECT geom FROM "{name}_05_tmp2"
            UNION ALL
            SELECT geom FROM "{name}_02a"
            WHERE fid IN (SELECT fid FROM "{name}_01_modified_fids")
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM lines
        )
        SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
        FROM noded
    """)
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02a"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02b"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp2"')

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
    #
    # The bbox prefilter on the LEFT JOIN ON clause is required: ST_Within alone
    # plans as SPATIAL_JOIN (~1x RAM virtual reservation). With explicit
    # ST_X/ST_Y(p.pt) vs ST_XMin/XMax/YMin/YMax(c.vgeom) comparisons, the
    # planner uses PIECEWISE_MERGE_JOIN with ST_Within applied as a residual
    # FILTER. The bbox predicates are necessary conditions for ST_Within so
    # adding them does not change semantics.
    #
    # Fallback for unmatched (extension) cells uses `_04` — the per-fid Voronoi
    # territory. Each unmatched cell's centroid is inside exactly one Voronoi
    # cell, giving the nearest fid in O(N+M) instead of O(N*M) from a
    # CROSS JOIN with ST_Distance ranking.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        WITH
        cells AS (
            SELECT ROW_NUMBER() OVER () AS cid, geom AS vgeom FROM "{name}_05_tmp3"
        ),
        all_joined AS (
            SELECT c.cid, c.vgeom, p.* EXCLUDE (pt)
            FROM cells c
            LEFT JOIN "{name}_05_tmp4" AS p
              ON ST_X(p.pt) >= ST_XMin(c.vgeom)
             AND ST_X(p.pt) <= ST_XMax(c.vgeom)
             AND ST_Y(p.pt) >= ST_YMin(c.vgeom)
             AND ST_Y(p.pt) <= ST_YMax(c.vgeom)
             AND ST_Within(p.pt, c.vgeom)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY c.cid ORDER BY p.fid NULLS LAST) = 1
        ),
        unmatched_centroids AS (
            SELECT cid, vgeom, ST_Centroid(vgeom) AS c_pt
            FROM all_joined WHERE fid IS NULL
        ),
        fallback AS (
            SELECT u.vgeom, p.* EXCLUDE (pt)
            FROM unmatched_centroids u
            JOIN "{name}_04" v
              ON ST_X(u.c_pt) >= ST_XMin(v.geom)
             AND ST_X(u.c_pt) <= ST_XMax(v.geom)
             AND ST_Y(u.c_pt) >= ST_YMin(v.geom)
             AND ST_Y(u.c_pt) <= ST_YMax(v.geom)
             AND ST_Within(u.c_pt, v.geom)
            JOIN "{name}_05_tmp4" p ON p.fid = v.fid
            QUALIFY ROW_NUMBER() OVER (PARTITION BY u.cid ORDER BY p.fid) = 1
        )
        SELECT * EXCLUDE (vgeom, cid), ST_Collect(list(vgeom)) AS geom
        FROM (
            SELECT * FROM all_joined WHERE fid IS NOT NULL
            UNION ALL
            SELECT NULL AS cid, * FROM fallback
        )
        GROUP BY ALL
    """)
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp4"')
