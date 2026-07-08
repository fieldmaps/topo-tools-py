"""Detects gap, overlap, and sliver defects in a single polygon layer.

Detection only -- no geometry is modified here. Ported from
topo-tools-js/src/lib/tools/topology-cleaner/pipeline/issues.ts:

- Gaps: interior rings of the whole-table union. Only catches fully-enclosed
  holes (a ring of polygons surrounding missing area) -- an open "inlet"
  between two non-enclosing polygons is not a gap by this definition (GEOS's
  own CoverageCleaner doc: "gaps which are not fully enclosed are not
  removed"); it surfaces as a sliver instead, if within tolerance.
- Overlaps: bbox-prefiltered pairwise ST_Intersection, whole-fid bboxes (not
  per-part -- see core/extend/_02_lines.py's neighbor self-join and
  docs/voronoi-memory.md for why per-part explosion regresses
  single-fid-many-parts datasets like Chile).
- Slivers: ST_CoverageInvalidEdges_Agg(geom, tolerance) flags near-miss
  boundary edges (within `tolerance`) not already explained by a detected
  gap/overlap region; those regions are subtracted first so a genuine
  overlap or enclosed gap isn't double-reported as a sliver too.

Each of the three detection queries is retried once at reduced precision on
failure, then falls back to an empty result (logged) rather than raising --
one kind failing shouldn't block the others, matching match's "failed group
is logged and dropped, not fatal" precedent.
"""

from collections.abc import Callable
from logging import getLogger

from duckdb import DuckDBPyConnection

from ._constants import MIN_ISSUE_AREA_M2, REDUCED_PRECISION_DEG
from ._units import METERS_PER_DEGREE, cos_lat_factor, m2_to_deg_sq, meters_to_degrees

logger = getLogger(__name__)


def centroid_lat_of(conn: DuckDBPyConnection, table: str) -> float:
    lat = conn.execute(f"""--sql
        SELECT ST_Y(ST_Centroid(ST_Extent_Agg(geom))) FROM "{table}"
    """).fetchall()[0][0]
    return lat if lat is not None else 0.0


def _run_with_retry(
    conn: DuckDBPyConnection,
    kind: str,
    source: str,
    build: Callable[[DuckDBPyConnection, str], None],
) -> None:
    """Call build(conn, source); on failure, retry once at reduced precision."""
    try:
        build(conn, source)
    except Exception as e:  # noqa: BLE001 -- GEOS topology failures surface as generic duckdb errors
        logger.warning(
            "%s detection failed on %s (%s), retrying at reduced precision",
            kind,
            source,
            e,
        )
    else:
        return
    reduced = f"{source}_reduced"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{reduced}" AS
        SELECT * EXCLUDE (geom),
               ST_ReducePrecision(geom, {REDUCED_PRECISION_DEG}) AS geom
        FROM "{source}"
    """)
    try:
        build(conn, reduced)
    except Exception as e:  # noqa: BLE001 -- see above
        logger.warning(
            "%s detection failed even at reduced precision (%s); reporting none",
            kind,
            e,
        )


def _build_gaps(
    conn: DuckDBPyConnection, tmp: str, table: str, min_area_deg2: float
) -> None:
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{tmp}" AS
        WITH union_cte AS (
            SELECT ST_Union_Agg(geom) AS u FROM "{table}"
            WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
        ),
        parts AS (
            SELECT (UNNEST(ST_Dump(u))).geom AS poly FROM union_cte WHERE u IS NOT NULL
        ),
        holes AS (
            SELECT UNNEST(ST_Dump(
                ST_Difference(ST_MakePolygon(ST_ExteriorRing(poly)), poly)
            )).geom AS geom
            FROM parts WHERE ST_NumInteriorRings(poly) > 0
        )
        SELECT row_number() OVER () AS n, geom
        FROM holes
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
          AND ST_Area(geom) > {min_area_deg2}
    """)


def _build_overlaps(
    conn: DuckDBPyConnection, tmp: str, table: str, min_area_deg2: float
) -> None:
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{tmp}" AS
        WITH pairs AS (
            SELECT a.fid AS unit_a, b.fid AS unit_b,
                   ST_MakeValid(
                       ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 3)
                   ) AS geom
            FROM "{table}" a JOIN "{table}" b
              ON a.fid < b.fid
              AND ST_XMax(b.geom) >= ST_XMin(a.geom)
              AND ST_XMin(b.geom) <= ST_XMax(a.geom)
              AND ST_YMax(b.geom) >= ST_YMin(a.geom)
              AND ST_YMin(b.geom) <= ST_YMax(a.geom)
              AND ST_Intersects(a.geom, b.geom)
        )
        SELECT row_number() OVER () AS n, unit_a, unit_b, geom
        FROM pairs
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
          AND ST_Area(geom) > {min_area_deg2}
    """)


def _build_slivers(  # noqa: PLR0913 -- each param is a distinct required input, not decomposable
    conn: DuckDBPyConnection,
    tmp: str,
    table: str,
    tol_deg: float,
    gaps_tmp: str,
    overlaps_tmp: str,
) -> None:
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{tmp}" AS
        WITH dumped AS (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}"),
        ie AS (SELECT ST_CoverageInvalidEdges_Agg(geom, {tol_deg}) AS e FROM dumped),
        buf AS (
            SELECT e, ST_Buffer(e, {tol_deg}) AS bg
            FROM ie WHERE e IS NOT NULL AND NOT ST_IsEmpty(e)
        ),
        clusters AS (SELECT e, (UNNEST(ST_Dump(bg))).geom AS blob FROM buf),
        ov AS (
            SELECT ST_Buffer(ST_Union_Agg(geom), {tol_deg}) AS g
            FROM "{overlaps_tmp}" WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
        ),
        gp AS (
            SELECT ST_Buffer(ST_Union_Agg(geom), {tol_deg}) AS g
            FROM "{gaps_tmp}" WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
        ),
        lines AS (
            SELECT CASE
                WHEN ov.g IS NOT NULL AND gp.g IS NOT NULL THEN
                    ST_Difference(
                        ST_Difference(ST_Intersection(c.e, c.blob), ov.g), gp.g
                    )
                WHEN ov.g IS NOT NULL THEN
                    ST_Difference(ST_Intersection(c.e, c.blob), ov.g)
                WHEN gp.g IS NOT NULL THEN
                    ST_Difference(ST_Intersection(c.e, c.blob), gp.g)
                ELSE ST_Intersection(c.e, c.blob)
            END AS geom
            FROM clusters c LEFT JOIN ov ON TRUE LEFT JOIN gp ON TRUE
        )
        SELECT row_number() OVER (ORDER BY ST_Length(geom) DESC) AS n, geom
        FROM lines
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    """)


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    sliver_tolerance_m: float,
    debug: bool = False,
) -> None:
    """Detect gap/overlap/sliver issues in `{name}_01`, writing `{name}_02`."""
    table = f"{name}_01"
    centroid_lat = centroid_lat_of(conn, table)
    min_area_deg2 = m2_to_deg_sq(MIN_ISSUE_AREA_M2, centroid_lat)
    cos_lat = cos_lat_factor(centroid_lat)

    gaps_tmp = f"{name}_02_tmp1"
    overlaps_tmp = f"{name}_02_tmp2"
    slivers_tmp = f"{name}_02_tmp3"

    _run_with_retry(
        conn, "gap", table, lambda c, t: _build_gaps(c, gaps_tmp, t, min_area_deg2)
    )
    _run_with_retry(
        conn,
        "overlap",
        table,
        lambda c, t: _build_overlaps(c, overlaps_tmp, t, min_area_deg2),
    )
    if sliver_tolerance_m > 0:
        tol_deg = meters_to_degrees(sliver_tolerance_m, centroid_lat)
        _run_with_retry(
            conn,
            "sliver",
            table,
            lambda c, t: _build_slivers(
                c, slivers_tmp, t, tol_deg, gaps_tmp, overlaps_tmp
            ),
        )
    else:
        empty_geom_sql = (
            f'CREATE OR REPLACE TABLE "{slivers_tmp}" AS '
            "SELECT NULL::GEOMETRY AS geom WHERE FALSE"
        )
        conn.execute(empty_geom_sql)

    # area_m2/max_width_m: area_deg2 * METERS_PER_DEGREE^2 * cos(centroid_lat) for
    # area; MIC diameter (deg) * METERS_PER_DEGREE (no cos factor -- matches
    # units.ts's degToM, exact for N-S widths, display-only approximation for E-W).
    m2_per_deg2 = METERS_PER_DEGREE**2 * cos_lat
    width_m = f"(ST_MaximumInscribedCircle(geom)).radius * 2 * {METERS_PER_DEGREE}"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT 'gap-' || n AS key, 'gap' AS kind,
               ST_Area(geom) * {m2_per_deg2} AS area_m2,
               {width_m} AS max_width_m,
               NULL::BIGINT AS unit_a, NULL::BIGINT AS unit_b, geom
        FROM "{gaps_tmp}"
        UNION ALL
        SELECT 'overlap-' || n AS key, 'overlap' AS kind,
               ST_Area(geom) * {m2_per_deg2} AS area_m2,
               {width_m} AS max_width_m,
               unit_a, unit_b, geom
        FROM "{overlaps_tmp}"
        UNION ALL
        SELECT 'sliver-' || n AS key, 'sliver' AS kind,
               NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
               NULL::BIGINT AS unit_a, NULL::BIGINT AS unit_b, geom
        FROM "{slivers_tmp}"
    """)

    if not debug:
        for tmp in (gaps_tmp, overlaps_tmp, slivers_tmp, f"{table}_reduced"):
            conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
