"""Coverage-topology helpers shared by the inputs and merge stages."""

from logging import getLogger

from duckdb import DuckDBPyConnection

logger = getLogger(__name__)


def has_coverage_violations(conn: DuckDBPyConnection, table: str) -> bool:
    """Return True if `table.geom` has any overlaps or unmatched shared edges."""
    return conn.execute(f"""--sql
        SELECT ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL
        FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
    """).fetchall()[0][0]


def check_overlaps(conn: DuckDBPyConnection, table: str) -> None:
    """Raise RuntimeError if `table.geom` has any overlaps or unmatched shared edges."""
    if has_coverage_violations(conn, table):
        error = f"OVERLAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def check_gaps(conn: DuckDBPyConnection, table: str) -> None:
    """Raise RuntimeError if the union of `table.geom` has any interior holes."""
    interior_rings = conn.execute(f"""--sql
        WITH u AS (
            SELECT ST_Union_Agg(geom) AS g
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
        )
        SELECT ST_NumInteriorRings(g)
        FROM u
    """).fetchall()[0][0]
    if (interior_rings or 0) > 0:
        error = f"GAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def coverage_clean(  # noqa: PLR0913 -- each param is a distinct required input, not decomposable
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    fids: list[int] | None,
    gap_max_width: float | None,
    snap_distance: float = -1.0,
) -> None:
    """Write table_out from table_in with ST_CoverageClean applied to a subset (or all).

    ``fids=None`` runs the clean over the entire table. ``gap_max_width=None``
    is equivalent to ``-1.0`` (GEOS's own no-op sentinel -- see
    GEOSCoverageCleanParams_setGapMaximumWidth: a negative value leaves the
    gap-merge width at its hardcoded 0.0 default, i.e. no gap-filling).
    ``snap_distance=-1.0`` (default) keeps GEOS's auto-computed snapping
    distance (extent diameter / 1e8); 0.0 disables snapping; a positive value
    overrides it. Always uses the 3-arg ST_CoverageClean overload -- verified
    functionally identical to the 1-arg/2-arg overloads for -1.0/omitted
    values, since DuckDB's own Bind() defaults missing args to -1.0.

    Mapping back to source rows: ST_CoverageClean returns a GeometryCollection
    whose i-th element corresponds to input i. ST_Dump recursively unnests
    sub-polygons of MultiPolygon elements, so we group by ``path[1]`` (the
    top-level index) and re-aggregate. A group of size 1 keeps the original
    Polygon type; a group of size >1 (MultiPolygon input/output) is re-wrapped
    via ST_Collect.
    """
    where = "" if fids is None else f"WHERE fid IN ({','.join(str(f) for f in fids)})"
    gap = -1.0 if gap_max_width is None else gap_max_width
    cc = f"ST_CoverageClean(list(geom ORDER BY fid), {snap_distance}, {gap})"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        WITH ord AS (
            SELECT fid, row_number() OVER (ORDER BY fid) AS rn
            FROM "{table_in}" {where}
        ),
        coll AS (
            SELECT {cc} AS g FROM "{table_in}" {where}
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
        FROM "{table_in}" t
        LEFT JOIN mapping m USING (fid)
    """)
