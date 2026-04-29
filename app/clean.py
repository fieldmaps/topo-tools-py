"""DuckDB-native equivalent of GEOS ST_CoverageClean.

Cleans coverage errors in `_01` by:

1. Identifying which interior holes of `ST_Union_Agg(_01)` are lakes (preserved)
   versus slivers (absorbed into a neighbour) without modifying any original
   geometry. Classification uses two scale-invariant gates (max-inscribed-circle
   diameter; Polsby-Popper compactness).

2. Turning `_01` into lines via `ST_Boundary`, then `ST_Node` + `ST_Polygonize`
   to produce atomic regions whose boundaries are made entirely of original
   polygon vertices (plus crossing vertices added by `ST_Node` — never moved,
   only inserted, and shared identically between every atom that touches the
   crossing).

3. Joining atoms with each polygon's representative point to assign each atom
   to a fid. Atoms that match multiple polygons (overlap regions) go to the
   `overlap_strategy` winner. Atoms that match no polygon are gaps — slivers
   go to their longest-border neighbour, lakes are skipped.

4. `ST_Union_Agg`-ing atoms per fid for the cleaned `_01`.

The crucial property versus a per-loser `ST_Difference` approach: every shared
edge between adjacent cleaned polygons inherits its coordinates from the same
`ST_Polygonize` output, so `lines.py`'s exact `ST_Intersection` matches them
without sub-pixel residue. Modifications to `_01` are only made where dirty
input topology demands them; clean coverages take the early-exit path and pass
through bit-for-bit.

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

# ORDER BY expression that picks the strategy winner per atom. References
# columns materialised in the candidates CTE: `p_area` and `shared_len`.
_STRATEGY_ORDER: dict[OverlapStrategy, str] = {
    "largest_area": "p_area",
    "merge_longest_border": "shared_len",
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
    """Clean coverage errors in `_01` via polygonize-and-reattribute.

    A hole is treated as a fillable sliver if EITHER:
      - its max-inscribed-circle diameter ≤ ``gap_maximum_width`` (small
        round artifact, sub-pixel safety net), OR
      - its Polsby-Popper compactness ``4πA/P²`` ≤ ``gap_max_thinness``
        (stringy/elongated shape, primary discriminator).

    Lakes and intentional small wedges are large AND compact — they fail
    both gates and are preserved as holes in the cleaned coverage.
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
    # Persistent tracker — merge.py uses this to know which polygons need
    # their full `_02a` exterior edges added to the polygonize input. Empty if
    # clean exited early (uniform interface).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_modified_fids" (fid INTEGER)
    """)

    if not has_errors:
        logger.info("clean: no coverage errors in %s_01, skipping", name)
        return

    strategy_order = _STRATEGY_ORDER[overlap_strategy]

    # Full snapshot of `_01` (attributes AND geometry). Geometry is preserved so
    # that any polygon which ends up with no assigned atoms (e.g. fully
    # contained by a strategy winner — unusual) can fall back to its original
    # geom rather than disappearing from the output.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp0" AS
        SELECT * FROM "{name}_01"
    """)

    # Lakes: interior rings of the union that are large AND compact (fail BOTH
    # the width and thinness gates). These are preserved as holes — atoms
    # falling inside them are not assigned to any fid.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp1" AS
        WITH unioned AS (
            SELECT ST_Union_Agg(geom) AS u FROM "{name}_01"
        ),
        shells AS (
            SELECT UNNEST(ST_Dump(u)).geom AS shell FROM unioned
        ),
        holes AS (
            SELECT ST_MakePolygon(ST_InteriorRingN(shell, n)) AS hole_geom
            FROM shells, generate_series(1, ST_NumInteriorRings(shell)) AS s(n)
            WHERE ST_NumInteriorRings(shell) > 0
        )
        SELECT hole_geom
        FROM holes
        WHERE NOT (
            2 * (ST_MaximumInscribedCircle(hole_geom)).radius
                <= {gap_maximum_width!r}
            OR 4 * pi() * ST_Area(hole_geom)
                / NULLIF(pow(ST_Perimeter(hole_geom), 2), 0)
                <= {gap_max_thinness!r}
        )
    """)

    # One representative point per polygon part. Multipolygons contribute one
    # row per part. Used to attribute each polygonize atom to a fid.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp2" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom
            FROM "{name}_01"
        )
        SELECT fid, ST_PointOnSurface(part_geom) AS pt
        FROM parts
    """)

    # Atomic regions from `ST_Node` + `ST_Polygonize` over every polygon's
    # boundary. ST_Node only ADDS crossing vertices — it never moves an
    # existing one — so unaffected polygon edges keep their original vertices
    # exactly, and every atom that touches a crossing inherits identical
    # coordinates from the same ST_Node output. Adjacent polygons therefore
    # share their cleaned-coverage boundaries vertex-for-vertex.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp3" AS
        WITH lines AS (
            SELECT ST_Boundary(geom) AS line FROM "{name}_01"
        ),
        noded AS (
            SELECT ST_Node(ST_Collect(list(line))) AS line FROM lines
        ),
        atoms AS (
            SELECT UNNEST(ST_Dump(ST_Polygonize(list(line)))).geom AS atom
            FROM noded
        )
        SELECT ROW_NUMBER() OVER () AS aid, atom FROM atoms
    """)

    # Atom-to-fid assignment.
    #
    # candidates: every (atom, fid) pair where the polygon's representative
    #   point is inside the atom. Multipolygon parts may produce multiple
    #   candidate rows for the same (aid, fid) — `DISTINCT` collapses them.
    #   `p_area` and `shared_len` are precomputed per row so the strategy
    #   ORDER BY can be a column reference (works inside QUALIFY).
    #
    # interior_winners: one row per atom that has at least one candidate. Picks
    #   the strategy winner (largest_area or merge_longest_border). Atoms with
    #   exactly one candidate trivially keep that candidate.
    #
    # gap_atoms: atoms with NO candidates. These are interior holes of the
    #   coverage (slivers OR lakes) plus any "outside the dataset" rings (rare
    #   for sane inputs).
    #
    # sliver_atoms: gap atoms that are NOT inside a preserved lake.
    #
    # sliver_assigned: each sliver goes to its longest-border neighbour.
    #   Hardcoded — `largest_area` is meaningless for gaps (a gap is between
    #   polygons, not contained in them).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp4" AS
        WITH
        candidates AS (
            SELECT DISTINCT a.aid, a.atom, pt.fid,
                ST_Area(p.geom) AS p_area,
                ST_Length(ST_Intersection(
                    ST_Boundary(a.atom), ST_Boundary(p.geom)
                )) AS shared_len
            FROM "{name}_01_tmp3" a
            JOIN "{name}_01_tmp2" pt
              ON ST_X(pt.pt) >= ST_XMin(a.atom)
             AND ST_X(pt.pt) <= ST_XMax(a.atom)
             AND ST_Y(pt.pt) >= ST_YMin(a.atom)
             AND ST_Y(pt.pt) <= ST_YMax(a.atom)
             AND ST_Within(pt.pt, a.atom)
            JOIN "{name}_01" p ON p.fid = pt.fid
        ),
        interior_winners AS (
            SELECT aid, atom, fid
            FROM candidates
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY aid ORDER BY {strategy_order} DESC, fid ASC
            ) = 1
        ),
        gap_atoms AS (
            SELECT a.aid, a.atom
            FROM "{name}_01_tmp3" a
            WHERE NOT EXISTS (
                SELECT 1 FROM "{name}_01_tmp2" pt
                WHERE ST_Within(pt.pt, a.atom)
            )
        ),
        sliver_atoms AS (
            SELECT g.aid, g.atom
            FROM gap_atoms g
            WHERE NOT EXISTS (
                SELECT 1 FROM "{name}_01_tmp1" lake
                WHERE ST_Within(ST_PointOnSurface(g.atom), lake.hole_geom)
            )
        ),
        sliver_assigned AS (
            SELECT s.aid, s.atom, p.fid
            FROM sliver_atoms s
            JOIN "{name}_01" p
              ON ST_XMax(s.atom) >= ST_XMin(p.geom)
             AND ST_XMin(s.atom) <= ST_XMax(p.geom)
             AND ST_YMax(s.atom) >= ST_YMin(p.geom)
             AND ST_YMin(s.atom) <= ST_YMax(p.geom)
             AND ST_Intersects(ST_Boundary(s.atom), ST_Boundary(p.geom))
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY s.aid ORDER BY
                    ST_Length(ST_Intersection(
                        ST_Boundary(s.atom), ST_Boundary(p.geom)
                    )) DESC,
                    p.fid ASC
            ) = 1
        )
        SELECT aid, atom, fid FROM interior_winners
        UNION ALL
        SELECT aid, atom, fid FROM sliver_assigned
    """)

    # Rewrite `_01`: union assigned atoms per fid, re-attach attributes. A
    # polygon with no assigned atoms (e.g. swallowed entirely by a larger
    # strategy winner — rare) falls back to its original geometry from the
    # snapshot, so no row is ever silently dropped here.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01" AS
        WITH per_fid AS (
            SELECT fid, ST_Union_Agg(atom) AS new_geom
            FROM "{name}_01_tmp4"
            GROUP BY fid
        )
        SELECT a.* EXCLUDE (geom),
               COALESCE(p.new_geom, a.geom) AS geom
        FROM "{name}_01_tmp0" a
        LEFT JOIN per_fid p ON a.fid = p.fid
    """)

    # Populate the persistent modified-fids tracker. A polygon is "modified"
    # if its cleaned geometry differs from the snapshot — covers losers, gap
    # winners, and any polygon affected by the polygonize+reattribute pass.
    conn.execute(f"""--sql
        INSERT INTO "{name}_01_modified_fids"
        SELECT a.fid
        FROM "{name}_01" a JOIN "{name}_01_tmp0" o ON a.fid = o.fid
        WHERE NOT ST_Equals(a.geom, o.geom)
    """)

    if not debug:
        for n in range(5):
            conn.execute(f'DROP TABLE IF EXISTS "{name}_01_tmp{n}"')
