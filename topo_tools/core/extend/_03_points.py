"""Creates interpolated points along boundary lines at configurable intervals."""

from decimal import Decimal

from duckdb import DuckDBPyConnection

from ._constants import MAX_POINTS_PER_SEGMENT, SNAP_TOLERANCE


def build_segments(conn: DuckDBPyConnection, name: str) -> None:
    """Decompose each real boundary line into its own real vertex-to-vertex segments.

    No geometry alteration — every downstream point stays exactly on the
    true digitized boundary. Independent of DISTANCE, so callers can build
    this once and reuse it across attempt.py's retry loop instead of
    recomputing it on every attempt.

    Vertices are extracted once via ST_Points/ST_Dump (path-ordered) and
    paired with LAG() rather than repeated ST_PointN(geom, i) calls:
    ST_PointN re-walks the geometry from its start on every call, which OOMs
    almost instantly on a large line (confirmed on a synthetic 500K-vertex
    line) — an O(n^2) blowup that would hit Chile-scale _02 rows hard. The
    window function is O(n log n). lid (per-_02-row, not fid) is required for
    PARTITION BY: a single fid can have multiple disjoint _02 line pieces
    (e.g. a multipolygon exterior), and partitioning by fid alone would
    wrongly stitch the last vertex of one piece to the first vertex of the
    next.
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp1" AS
        WITH lines AS (
            SELECT row_number() OVER () AS lid, fid, geom
            FROM "{name}_02"
        ), verts AS (
            SELECT
                lid, fid,
                UNNEST(ST_Dump(ST_Points(geom))).geom AS geom,
                UNNEST(ST_Dump(ST_Points(geom))).path[1] AS idx
            FROM lines
        )
        SELECT
            fid,
            ST_MakeLine(prev_geom, geom) AS geom,
            ST_Distance(prev_geom, geom) AS seg_len
        FROM (
            SELECT
                fid, geom,
                LAG(geom) OVER (PARTITION BY lid ORDER BY idx) AS prev_geom
            FROM verts
        )
        WHERE prev_geom IS NOT NULL
    """)


def main(
    conn: DuckDBPyConnection, name: str, distance: Decimal, *, debug: bool = False
) -> None:
    """Create points along boundary lines.

    Assumes build_segments has already created "{name}_03_tmp1".
    """
    d = float(distance)
    cap_threshold = d * MAX_POINTS_PER_SEGMENT

    # Buffered union of all line endpoints — marks the shared-boundary zone
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03a" AS
        SELECT ST_Union_Agg(ST_Buffer(ST_Boundary(geom), {SNAP_TOLERANCE}))
            AS geom
        FROM "{name}_02"
    """)

    # Split into "long" real segments (rare — a single one can span many
    # degrees, e.g. Chad/Algeria's straight desert admin lines) and "normal"
    # ones. Long segments get capped interpolation directly. Normal segments
    # are re-merged back into contiguous per-fid lines and resampled with the
    # original whole-line formula — this matters because decomposing into
    # per-segment points unconditionally (even earlier revisions of this fix)
    # guarantees at least one point per real segment, creating a floor equal
    # to the file's raw vertex count. That floor doesn't respond to DISTANCE
    # and broke phl_admin3 (13M real vertices in its exterior boundary alone,
    # already over MAX_POINTS with zero interpolation) — every retry from
    # 0.0002 to 0.1024 kept failing near 13.07M points. Re-merging normal
    # segments before resampling restores the old arc-length behavior, which
    # can shrink below the raw vertex count as DISTANCE grows, for the
    # (overwhelming, in practice) majority of segments that were never the
    # pathological case to begin with.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp2" AS
        SELECT
            fid,
            ST_LineInterpolatePoints(
                geom,
                GREATEST(
                    LEAST({d!r} / seg_len, 1.0),
                    1.0 / {MAX_POINTS_PER_SEGMENT}
                ),
                true
            ) AS geom
        FROM "{name}_03_tmp1"
        WHERE seg_len > {cap_threshold!r}
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp3" AS
        SELECT
            fid,
            ST_LineInterpolatePoints(
                geom,
                LEAST({d!r} / ST_Length(geom), 1.0),
                true
            ) AS geom
        FROM (
            SELECT fid, UNNEST(ST_Dump(ST_LineMerge(ST_Union_Agg(geom)))).geom
                AS geom
            FROM "{name}_03_tmp1"
            WHERE seg_len <= {cap_threshold!r}
            GROUP BY fid
        )
    """)

    # Points from both branches, aggregated to one multipoint per fid
    # *before* differencing against the shared-boundary zone — differencing
    # per segment instead of per fid blew up on idn_admin3 (7,069 features,
    # 2.49M real segments): 12.7GB OOM at every retry distance, since it
    # repeats ST_Difference against the file-wide shared-boundary geometry
    # ~240x more often than the original per-line-row granularity.
    # Aggregating first restores that original per-fid call count regardless
    # of segment count.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp4" AS
        SELECT fid, ST_Union_Agg(geom) AS geom FROM (
            SELECT fid, geom FROM "{name}_03_tmp2"
            UNION ALL
            SELECT fid, geom FROM "{name}_03_tmp3"
        )
        GROUP BY fid
    """)

    # Points from above minus the shared-boundary zone, union'd with the line
    # endpoints also minus the shared-boundary zone
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03b" AS
        SELECT fid, geom FROM (
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Difference(a.geom, b.geom))).geom AS geom
            FROM "{name}_03_tmp4" AS a
            CROSS JOIN "{name}_03a" AS b
            UNION ALL
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Boundary(
                    ST_Difference(a.geom, b.geom)
                ))).geom AS geom
            FROM "{name}_02" AS a
            CROSS JOIN "{name}_03a" AS b
        )
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp2"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp3"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp4"')
