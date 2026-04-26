"""Validates topology by checking for overlaps, gaps, and missing rows."""

from logging import getLogger

from duckdb import DuckDBPyConnection

logger = getLogger(__name__)

# Sub-square-centimeter threshold for floating-point geometry artifacts (~0.1 m²).
AREA_EPSILON = 1e-10


def check_overlaps(conn: DuckDBPyConnection, table: str) -> None:
    """Check for overlapping polygons.

    Runs both a strict coverage-edge check and an area-based check, logs when
    they disagree, then raises on the area result (the authoritative one).
    """
    strict = conn.execute(f"""--sql
        SELECT ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL
        FROM "{table}"
    """).fetchall()[0][0]

    area_based = conn.execute(f"""--sql
        SELECT COUNT(*) > 0
        FROM "{table}" AS a
        JOIN "{table}" AS b ON a.fid < b.fid
        WHERE ST_Area(ST_Intersection(a.geom, b.geom)) > {AREA_EPSILON}
    """).fetchall()[0][0]

    if strict != area_based:
        logger.warning(
            "OVERLAPS check disagreement on %s: "
            "strict (CoverageInvalidEdges)=%s, area-based=%s",
            table,
            strict,
            area_based,
        )

    if area_based:
        error = f"OVERLAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def check_gaps(conn: DuckDBPyConnection, table: str) -> None:
    """Check for gaps in polygon coverage.

    Runs both a strict interior-ring count and an area-based check, logs when
    they disagree, then raises on the area result (the authoritative one).
    """
    strict_rings, gap_area = conn.execute(f"""--sql
        WITH u AS (
            SELECT ST_Union_Agg(geom) AS g, ST_Extent_Agg(geom) AS ext
            FROM "{table}"
        )
        SELECT ST_NumInteriorRings(g), ST_Area(ST_Difference(ext, g))
        FROM u
    """).fetchone()
    strict = (strict_rings or 0) > 0
    gap_area = gap_area or 0.0
    area_based = gap_area > AREA_EPSILON

    if strict != area_based:
        logger.warning(
            "GAPS check disagreement on %s: "
            "strict (NumInteriorRings)=%s, area-based (area=%.2e)=%s",
            table,
            strict,
            gap_area,
            area_based,
        )

    if area_based:
        error = f"GAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def check_missing_rows(conn: DuckDBPyConnection, table_1: str, table_2: str) -> None:
    """Check that two tables have the same row count."""
    rows_1 = conn.execute(f'SELECT count(*) FROM "{table_1}"').fetchall()[0][0] or 0
    rows_2 = conn.execute(f'SELECT count(*) FROM "{table_2}"').fetchall()[0][0] or 0
    if rows_1 != rows_2:
        error = (
            f"MISSING ROWS: {table_1} has {rows_1} rows, "
            f"but {table_2} has {rows_2} rows"
        )
        logger.error(error)
        raise RuntimeError(error)
