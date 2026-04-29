"""DuckDB-native equivalent of GEOS ST_CoverageClean.

Removes overlap pairs (subtracted from the loser, decided by `overlap_strategy`)
and absorbs thin-sliver gaps into their longest-border neighbour. Wide interior
holes (lakes, enclaves) are preserved untouched.

This module is the swap boundary: when DuckDB-spatial wraps GEOS 3.13's
`coverage_clean`, the body of `main` collapses to a single
`SELECT ST_CoverageClean(list(geom), ...)` call. Signature mirrors PostGIS
`ST_CoverageClean(geom_array, snapping_distance, gap_maximum_width,
overlap_merge_strategy)` so the swap is a body-only change.
"""

from logging import getLogger
from typing import Literal

from duckdb import DuckDBPyConnection

from .config import debug

OverlapStrategy = Literal["largest_area", "merge_longest_border"]

_LOSER_EXPR: dict[OverlapStrategy, str] = {
    "largest_area": "CASE WHEN aarea >= barea THEN bfid ELSE afid END",
    "merge_longest_border": "CASE WHEN aborder >= bborder THEN bfid ELSE afid END",
}

logger = getLogger(__name__)


def main(  # noqa: PLR0913 - mirrors ST_CoverageClean signature
    conn: DuckDBPyConnection,
    name: str,
    *,
    snapping_distance: float = 0.0,  # noqa: ARG001 - reserved for ST_CoverageClean swap
    gap_maximum_width: float = 0.0001,
    gap_max_thinness: float = 0.05,
    overlap_strategy: OverlapStrategy = "largest_area",
) -> None:
    """Clean coverage errors in `_01`: overlap subtraction + thin-gap fill.

    A hole is treated as a fillable sliver if EITHER:
      - its max-inscribed-circle diameter ≤ ``gap_maximum_width`` (small
        round artifact, sub-pixel safety net), OR
      - its Polsby-Popper compactness ``4πA/P²`` ≤ ``gap_max_thinness``
        (stringy/elongated shape, primary discriminator).

    Lakes and intentional small wedges are large AND compact — they fail
    both gates and are preserved.
    """
    # Early exit when the input is already a valid coverage. ST_CoverageInvalidEdges
    # flags overlaps and unmatched shared edges (sliver-gap boundaries) but does
    # NOT flag legitimate interior holes like lakes, so a dataset whose only
    # holes are lakes returns NULL here and skips cleaning.
    has_errors = conn.execute(f"""--sql
        WITH agg AS (
            SELECT ST_CoverageInvalidEdges_Agg(geom) AS g FROM "{name}_01"
        )
        SELECT g IS NOT NULL AND NOT ST_IsEmpty(g) FROM agg
    """).fetchone()[0]
    if not has_errors:
        logger.info("clean: no coverage errors in %s_01, skipping", name)
        return

    loser_expr = _LOSER_EXPR[overlap_strategy]

    # Snapshot non-geom attributes for re-join after the in-place rewrite.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp0" AS
        SELECT * EXCLUDE (geom) FROM "{name}_01"
    """)

    # Overlap pairs. Materialize both metrics (areas + per-side shared-border
    # length with the overlap region) so the strategy choice is a tiny SQL swap
    # downstream, not a re-detection. Bbox prefilter keeps the planner off
    # SPATIAL_JOIN. ST_CollectionExtract(..., 3) drops touching-only intersections
    # (linestrings) so area filter rejects them cleanly.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp1" AS
        WITH pairs AS (
            SELECT a.fid AS afid, a.geom AS ageom,
                   b.fid AS bfid, b.geom AS bgeom,
                   ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 3)
                       AS overlap_geom
            FROM "{name}_01" a JOIN "{name}_01" b
              ON a.fid < b.fid
             AND ST_XMax(a.geom) >= ST_XMin(b.geom)
             AND ST_XMin(a.geom) <= ST_XMax(b.geom)
             AND ST_YMax(a.geom) >= ST_YMin(b.geom)
             AND ST_YMin(a.geom) <= ST_YMax(b.geom)
             AND ST_Intersects(a.geom, b.geom)
        )
        SELECT afid, bfid, overlap_geom,
               ST_Area(ageom) AS aarea, ST_Area(bgeom) AS barea,
               ST_Length(ST_Intersection(
                   ST_Boundary(ageom), ST_Boundary(overlap_geom)
               )) AS aborder,
               ST_Length(ST_Intersection(
                   ST_Boundary(bgeom), ST_Boundary(overlap_geom)
               )) AS bborder
        FROM pairs
        WHERE ST_Area(overlap_geom) > 0
    """)

    # Per-loser cumulative loss. The strategy expression decides which side of
    # each overlap pair loses the area. `>=` makes b lose on ties, which is
    # deterministic and dataset-order-independent.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp2" AS
        SELECT loser_fid, ST_Union_Agg(overlap_geom) AS loss_geom
        FROM (
            SELECT {loser_expr} AS loser_fid, overlap_geom
            FROM "{name}_01_tmp1"
        )
        GROUP BY loser_fid
    """)

    # Apply losses. Winners pass through untouched (LEFT JOIN miss → loss_geom
    # NULL → COALESCE keeps original geom byte-for-byte). Losers get the union
    # of all their losses subtracted in one ST_Difference call.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp3" AS
        SELECT p.fid,
               COALESCE(ST_Difference(p.geom, l.loss_geom), p.geom) AS geom
        FROM "{name}_01" p
        LEFT JOIN "{name}_01_tmp2" l ON p.fid = l.loser_fid
    """)

    # Candidate gaps = interior rings of ST_Union_Agg. Two-gate sliver filter:
    #
    #   - ``max_w`` (max-inscribed-circle diameter): scale-dependent. Catches
    #     sub-pixel ST_Difference cut artifacts that are too small to be real
    #     features regardless of shape. Default ≈ 11 m at equator.
    #
    #   - ``pp`` (Polsby-Popper, 4πA/P²): scale-INVARIANT. 1.0 = circle,
    #     near-0 = stringy. A vertex displaced 15 m on a 1 km shared edge
    #     produces a sliver with PP ≈ 0.024; default 0.05 catches up to a
    #     ~1:30 aspect ratio, well clear of any compact intentional shape
    #     (squares ≈ 0.79, equilateral triangles ≈ 0.60).
    #
    # A hole is a sliver iff EITHER gate trips. Lakes (large + compact) and
    # intentional small wedges (small + compact) fail both → preserved.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp4" AS
        WITH unioned AS (
            SELECT ST_Union_Agg(geom) AS u FROM "{name}_01_tmp3"
        ),
        shells AS (
            SELECT UNNEST(ST_Dump(u)).geom AS shell FROM unioned
        ),
        holes AS (
            SELECT ST_MakePolygon(ST_InteriorRingN(shell, n)) AS gap_geom
            FROM shells, generate_series(1, ST_NumInteriorRings(shell)) AS s(n)
            WHERE ST_NumInteriorRings(shell) > 0
        ),
        classified AS (
            SELECT gap_geom,
                2 * (ST_MaximumInscribedCircle(gap_geom)).radius AS max_w,
                4 * pi() * ST_Area(gap_geom)
                    / NULLIF(pow(ST_Perimeter(gap_geom), 2), 0) AS pp
            FROM holes
        )
        SELECT ROW_NUMBER() OVER () AS gap_id, gap_geom
        FROM classified
        WHERE max_w <= {gap_maximum_width!r}
           OR pp <= {gap_max_thinness!r}
    """)

    # Assign each surviving sliver to its longest-border neighbour. Hardcoded —
    # for gaps, "longest shared boundary" is the only intrinsic measurement;
    # "largest area" is meaningless because the gap is between polygons, not
    # contained in them.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp5" AS
        WITH neighbours AS (
            SELECT g.gap_id, g.gap_geom, p.fid,
                   ST_Length(ST_Intersection(
                       ST_Boundary(g.gap_geom), ST_Boundary(p.geom)
                   )) AS shared_len
            FROM "{name}_01_tmp4" g
            JOIN "{name}_01_tmp3" p
              ON ST_XMax(g.gap_geom) >= ST_XMin(p.geom)
             AND ST_XMin(g.gap_geom) <= ST_XMax(p.geom)
             AND ST_YMax(g.gap_geom) >= ST_YMin(p.geom)
             AND ST_YMin(g.gap_geom) <= ST_YMax(p.geom)
             AND ST_Intersects(ST_Boundary(g.gap_geom), ST_Boundary(p.geom))
        ),
        winners AS (
            SELECT gap_id, gap_geom, fid AS winner_fid
            FROM neighbours
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY gap_id ORDER BY shared_len DESC, fid ASC
            ) = 1
        )
        SELECT winner_fid, ST_Union_Agg(gap_geom) AS gain_geom
        FROM winners
        GROUP BY winner_fid
    """)

    # Rewrite _01 in place: re-attach attributes, apply gap gains.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01" AS
        SELECT a.*,
               CASE WHEN g.gain_geom IS NOT NULL
                    THEN ST_Union(p.geom, g.gain_geom)
                    ELSE p.geom
               END AS geom
        FROM "{name}_01_tmp3" p
        JOIN "{name}_01_tmp0" a ON p.fid = a.fid
        LEFT JOIN "{name}_01_tmp5" g ON p.fid = g.winner_fid
    """)

    if not debug:
        for n in range(6):
            conn.execute(f'DROP TABLE IF EXISTS "{name}_01_tmp{n}"')
