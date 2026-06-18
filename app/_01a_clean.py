"""Cleans coverage topology violations in _01 via DuckDB ST_CoverageClean.

Runs only when ST_CoverageInvalidEdges_Agg detects topology violations on
_01 or _sliver_info finds narrow interior-ring gaps; otherwise the stage
is a no-op.

When the coverage contains sliver gaps (interior rings with low Polsby-
Popper compactness), gap_max_width is auto-derived from the data: it
catches every thin gap while preserving any round feature (lake, etc.).
Compactness < 0.5 separates slivers from real features cleanly because
the lowest plausible compactness for a real feature is an equilateral
triangle at ~0.6.
"""

from logging import getLogger

from duckdb import DuckDBPyConnection

from .utils import has_coverage_violations

logger = getLogger(__name__)

_SLIVER_COMPACTNESS_MAX = 0.5
# Maximum sliver width as a fraction of the input coverage's bbox diagonal.
# Digitization noise scales with the spatial extent of the dataset (a Congo-
# scale layer can have 30m slivers; a city-scale layer has cm-scale ones), so
# the threshold has to scale too. A flat absolute would either miss real
# slivers in continental data or fill real holes in small-extent data. 1e-5
# is calibrated against ~30m slivers in Congo (bbox diagonal ~3000km).
_SLIVER_MAX_WIDTH_FRACTION = 1e-5


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Clean coverage topology violations and sliver gaps in _01 surgically.

    Identifies the subset of polygons that own invalid edges or border a
    sliver gap, runs ST_CoverageClean on that subset only, and splices the
    result back into _01. Polygons not in the subset stay byte-identical
    to input.

    Sliver gaps (interior rings in the unioned coverage with very low
    Polsby-Popper compactness) are not invalid edges, so
    ST_CoverageInvalidEdges_Agg won't flag them. They still break the
    pipeline downstream (lines.main can't extract a shared edge that
    doesn't exist as coincident geometry, and merge.main fuses the
    sliver-bounding polygons into one cell), so they need cleaning too.

    If the surgical pass leaves residual violations (e.g. cleaning created
    new ones at the boundary with unchanged neighbours), restore _01 from
    a snapshot and fall back to a single full-coverage clean — better to
    take the global drift hit once than ship a half-cleaned coverage.
    """
    invalid_fids = (
        _violator_fids(conn, name)
        if has_coverage_violations(conn, f"{name}_01")
        else []
    )
    gap_max_width, sliver_fids = _sliver_info(conn, name)
    violators = sorted(set(invalid_fids) | set(sliver_fids))
    if not violators:
        return

    conn.execute(
        f'CREATE OR REPLACE TABLE "{name}_01_snapshot" AS SELECT * FROM "{name}_01"'
    )
    logger.info(
        "surgical clean: %d violator(s) (%d invalid-edge, %d sliver) via "
        "ST_CoverageClean",
        len(violators),
        len(invalid_fids),
        len(sliver_fids),
    )
    _coverage_clean_subset(conn, name, violators, gap_max_width)

    residual_invalid = has_coverage_violations(conn, f"{name}_01")
    _, residual_slivers = _sliver_info(conn, name)
    if residual_invalid or residual_slivers:
        logger.warning(
            "surgical clean did not converge (invalid_edges=%s, slivers=%d); "
            "restoring _01 and falling back to full-coverage clean",
            residual_invalid,
            len(residual_slivers),
        )
        conn.execute(
            f'CREATE OR REPLACE TABLE "{name}_01" AS SELECT * FROM "{name}_01_snapshot"'
        )
        _global_clean(conn, name)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_01_snapshot"')


def _global_clean(conn: DuckDBPyConnection, name: str) -> None:
    """Run ST_CoverageClean on the entire _01 in one pass and write back.

    Used as a fallback when surgical cleaning fails to converge. Every
    polygon in _01 is sent through coverage clean, so every polygon's
    coordinates may shift.
    """
    gap_max_width, _ = _sliver_info(conn, name)
    nrows = conn.execute(f'SELECT count(*) FROM "{name}_01"').fetchone()[0]
    logger.info("global clean: %d feature(s) via ST_CoverageClean", nrows)
    _coverage_clean_subset(conn, name, None, gap_max_width)


def _coverage_clean_subset(
    conn: DuckDBPyConnection,
    name: str,
    fids: list[int] | None,
    gap_max_width: float | None,
) -> None:
    """Rewrite _01 with ST_CoverageClean applied to a subset (or all) fids.

    ``fids=None`` runs the clean over the entire table. ``gap_max_width``
    enables the gap-merge parameter on the 3-arg overload; otherwise the
    1-arg overload is used.

    Mapping back to source rows: ST_CoverageClean returns a GeometryCollection
    whose i-th element corresponds to input i. ST_Dump recursively unnests
    sub-polygons of MultiPolygon elements, so we group by ``path[1]`` (the
    top-level index) and re-aggregate. A group of size 1 keeps the original
    Polygon type; a group of size >1 (MultiPolygon input/output) is re-wrapped
    via ST_Collect.
    """
    where = "" if fids is None else f"WHERE fid IN ({','.join(str(f) for f in fids)})"
    # snap_distance=-1 keeps GEOS's auto-detect default (which the 1-arg
    # overload also uses); 0.0 would explicitly disable snapping and produce
    # different output than the prior ctypes path that left the param unset.
    cc = (
        "ST_CoverageClean(list(geom ORDER BY fid))"
        if gap_max_width is None
        else f"ST_CoverageClean(list(geom ORDER BY fid), -1.0, {gap_max_width})"
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01" AS
        WITH ord AS (
            SELECT fid, row_number() OVER (ORDER BY fid) AS rn
            FROM "{name}_01" {where}
        ),
        coll AS (
            SELECT {cc} AS g FROM "{name}_01" {where}
        ),
        dumped AS (
            SELECT (d).path[1] AS rn, (d).geom AS sub
            FROM (SELECT UNNEST(ST_Dump(g)) AS d FROM coll)
        ),
        grouped AS (
            SELECT rn, list(sub) AS subs FROM dumped GROUP BY rn
        ),
        parts AS (
            SELECT rn,
                   CASE WHEN len(subs) = 1 THEN subs[1] ELSE ST_Collect(subs) END
                       AS cleaned_geom
            FROM grouped
        ),
        mapping AS (
            SELECT ord.fid, parts.cleaned_geom
            FROM ord JOIN parts USING (rn)
        )
        SELECT t.* EXCLUDE (geom),
               COALESCE(m.cleaned_geom, t.geom) AS geom
        FROM "{name}_01" t
        LEFT JOIN mapping m USING (fid)
    """)


def _violator_fids(conn: DuckDBPyConnection, name: str) -> list[int]:
    """Return fids of polygons that touch any invalid coverage edge.

    `ST_CoverageInvalidEdges_Agg` returns a multilinestring whose non-empty
    parts are the edges where the coverage breaks. A polygon "owns" a
    violation if its boundary intersects any non-empty invalid edge.
    """
    return [
        r[0]
        for r in conn.execute(f"""--sql
        WITH
        bad AS (
            SELECT ST_CoverageInvalidEdges_Agg(geom) AS edges
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{name}_01")
        ),
        edges AS (
            SELECT UNNEST(ST_Dump(edges)).geom AS edge FROM bad
        ),
        real_edges AS (
            SELECT edge FROM edges
            WHERE NOT ST_IsEmpty(edge) AND ST_NPoints(edge) > 0
        )
        SELECT DISTINCT i.fid
        FROM "{name}_01" i, real_edges e
        WHERE ST_Intersects(i.geom, e.edge)
        ORDER BY i.fid
    """).fetchall()
    ]


def _sliver_info(conn: DuckDBPyConnection, name: str) -> tuple[float | None, list[int]]:
    """Detect sliver gaps; return (max_width, bordering_fids).

    A sliver gap is an interior ring of ST_Union_Agg(_01) that is BOTH
    irregularly-shaped (Polsby-Popper compactness < 0.5, below the lowest
    plausible compactness for a real polygonal feature — equilateral triangle
    ≈ 0.6) AND narrow relative to the dataset's spatial extent (max width
    under _SLIVER_MAX_WIDTH_FRACTION of the union's bbox diagonal). The
    width threshold scales with the bbox diagonal because digitization noise
    scales with the dataset: a Congo-sized layer can have 30m slivers; a
    city-sized layer has cm-scale ones. Real internal holes (lakes, enclaves,
    disputed territories) sit well above this scaled threshold and must be
    left as gaps for the Voronoi extension to divide across bordering
    polygons — filling them here collapses the whole hole onto one neighbour.

    The union is unnested into single-polygon parts before counting interior
    rings; ST_NumInteriorRings on a MultiPolygon returns 0, so a coverage
    that splits into multiple parts (e.g. mainland + offshore islet) would
    otherwise hide every interior-ring gap.

    Returns (None, []) when no slivers exist.
    """
    row = conn.execute(f"""--sql
        WITH
        u AS (SELECT ST_Union_Agg(geom) AS g FROM "{name}_01"),
        extent AS (
            SELECT sqrt(power(ST_XMax(g) - ST_XMin(g), 2)
                      + power(ST_YMax(g) - ST_YMin(g), 2))
                   * {_SLIVER_MAX_WIDTH_FRACTION} AS max_sliver_width
            FROM u
        ),
        parts AS (SELECT UNNEST(ST_Dump(g)).geom AS p FROM u),
        rings AS (
            SELECT UNNEST(generate_series(1, ST_NumInteriorRings(p))) AS i, p
            FROM parts
        ),
        gaps AS (
            SELECT ST_MakePolygon(ST_InteriorRingN(p, i)) AS gap FROM rings
        ),
        slivers AS (
            SELECT gap, width FROM (
                SELECT
                    gap,
                    ST_MaximumInscribedCircle(gap, 1e-9).radius * 2 AS width
                FROM gaps
                WHERE 4 * pi() * ST_Area(gap)
                    / (ST_Perimeter(gap) * ST_Perimeter(gap))
                    < {_SLIVER_COMPACTNESS_MAX}
            )
            WHERE width < (SELECT max_sliver_width FROM extent)
        )
        SELECT max(s.width), list(DISTINCT i.fid)
        FROM slivers s, "{name}_01" i
        WHERE ST_Intersects(i.geom, s.gap)
    """).fetchone()
    max_width, fids = row
    if max_width is None:
        return None, []
    fids = sorted(fids or [])
    logger.info(
        "auto-detected sliver gap(s) bordering %d feature(s); gap_max_width=%.3e",
        len(fids),
        max_width,
    )
    return float(max_width), fids
