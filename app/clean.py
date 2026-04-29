"""DuckDB-native equivalent of GEOS ST_CoverageClean.

Polygonize-and-reattribute: ST_Node + ST_Polygonize the union of polygon
boundaries into atoms, assign each atom to a fid via point-in-polygon, then
ST_Union_Agg per fid. ST_Node only inserts crossing vertices — never moves
existing ones — so adjacent cleaned polygons share boundaries
vertex-for-vertex, and lines.py's exact ST_Intersection matches them without
sub-pixel residue.

Swap boundary: when DuckDB-spatial wraps GEOS 3.13's coverage_clean, the body
collapses to `SELECT ST_CoverageClean(list(geom), ...)`. Signature mirrors
PostGIS ST_CoverageClean(geom_array, snapping_distance, gap_maximum_width,
overlap_merge_strategy).
"""

from logging import getLogger
from typing import Literal

from duckdb import DuckDBPyConnection

from .config import debug

OverlapStrategy = Literal["largest_area", "merge_longest_border"]

# merge_longest_border's ST_Intersection(ST_Boundary, ST_Boundary) dominates
# the candidates CTE on dirty data; skip computing it under largest_area.
_STRATEGY_METRIC: dict[OverlapStrategy, str] = {
    "largest_area": "ST_Area(p.geom)",
    "merge_longest_border": (
        "ST_Length(ST_Intersection(ST_Boundary(a.atom), ST_Boundary(p.geom)))"
    ),
}

logger = getLogger(__name__)


def main(  # noqa: PLR0913 - mirrors ST_CoverageClean signature
    conn: DuckDBPyConnection,
    name: str,
    *,
    snapping_distance: float = 0.0,  # noqa: ARG001 - reserved for ST_CoverageClean swap
    gap_maximum_width: float = 0.0001,
    gap_max_thinness: float = 0.05,
    overlap_strategy: OverlapStrategy = "merge_longest_border",
) -> None:
    """Clean coverage errors in `_01` via polygonize-and-reattribute.

    A hole is absorbed as a sliver if either its max-inscribed-circle diameter
    ≤ ``gap_maximum_width`` or its Polsby-Popper compactness ``4πA/P²``
    ≤ ``gap_max_thinness``. Lakes (large AND compact) fail both gates and are
    preserved.
    """
    # ST_CoverageInvalidEdges_Agg flags overlaps and unmatched shared edges,
    # not legitimate interior holes — lake-only datasets return NULL here.
    has_errors = conn.execute(f"""--sql
        WITH agg AS (
            SELECT ST_CoverageInvalidEdges_Agg(geom) AS g FROM "{name}_01"
        )
        SELECT g IS NOT NULL AND NOT ST_IsEmpty(g) FROM agg
    """).fetchall()[0][0]
    # merge.py reads this to know which fids need their full _02a exterior
    # edges in the polygonize input. Created empty so the contract holds even
    # when we exit early.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_modified_fids" (fid INTEGER)
    """)

    if not has_errors:
        return
    logger.info("clean: coverage errors detected in %s_01, cleaning", name)

    strategy_metric = _STRATEGY_METRIC[overlap_strategy]

    # Full snapshot — used for attribute reattach and as fallback geometry for
    # any fid that ends up with no assigned atoms.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp0" AS
        SELECT * FROM "{name}_01"
    """)

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

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp2" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom
            FROM "{name}_01"
        )
        SELECT fid, ST_PointOnSurface(part_geom) AS pt
        FROM parts
    """)

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

    # DISTINCT collapses multipart polygons that yield multiple (aid, fid) rows.
    # Sliver assignment always uses longest shared boundary — gaps live between
    # polygons, so "largest area" is meaningless there. bbox prefilters avoid
    # SPATIAL_JOIN OOM; see merge.py.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_tmp4" AS
        WITH
        candidates AS (
            SELECT DISTINCT a.aid, a.atom, pt.fid,
                {strategy_metric} AS metric
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
                PARTITION BY aid ORDER BY metric DESC, fid ASC
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

    # COALESCE keeps the original geometry for any fid with no assigned atoms
    # (e.g. fully swallowed by a strategy winner) so no row is silently dropped.
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

    conn.execute(f"""--sql
        INSERT INTO "{name}_01_modified_fids"
        SELECT a.fid
        FROM "{name}_01" a JOIN "{name}_01_tmp0" o ON a.fid = o.fid
        WHERE NOT ST_Equals(a.geom, o.geom)
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01_tmp0"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01_tmp1"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01_tmp2"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01_tmp3"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01_tmp4"')
