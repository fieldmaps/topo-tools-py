"""Coverage-topology helpers shared by the inputs and merge stages."""

from duckdb import DuckDBPyConnection


def has_coverage_violations(conn: DuckDBPyConnection, table: str) -> bool:
    """Return True if `table.geom` has any overlaps or unmatched shared edges."""
    return conn.execute(f"""--sql
        SELECT ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL
        FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
    """).fetchall()[0][0]


def coverage_clean(
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    fids: list[int] | None,
    gap_max_width: float | None,
) -> None:
    """Write table_out from table_in with ST_CoverageClean applied to a subset (or all).

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
