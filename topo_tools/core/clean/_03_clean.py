"""Fixes gap/overlap defects with ST_CoverageClean; slivers are never touched.

Overlaps are always fixed unconditionally by ST_CoverageClean itself -- no
flag controls that. gap_width and snap_tolerance are the only tunables (see
Decisions in the plan / docs/clean.md for why gap_maximum_width has no
GEOS-native "auto-fill" default, unlike snapping_distance).
"""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.extend._coverage import coverage_clean, has_coverage_violations

from ._02_issues import centroid_lat_of
from ._constants import ALL_GAP_WIDTH_EPSILON_FACTOR, REDUCED_PRECISION_DEG
from ._units import meters_to_degrees

logger = getLogger(__name__)


def _resolve_gap_max_width_deg(
    conn: DuckDBPyConnection,
    name: str,
    gap_width: tuple[str, float | None],
    centroid_lat: float,
) -> float:
    mode, value = gap_width
    if mode == "auto":
        return -1.0
    if mode == "all":
        widest_m = conn.execute(f"""--sql
            SELECT MAX(max_width_m) FROM "{name}_02" WHERE kind = 'gap'
        """).fetchall()[0][0]
        if widest_m is None:
            return -1.0
        return meters_to_degrees(widest_m * ALL_GAP_WIDTH_EPSILON_FACTOR, centroid_lat)
    return meters_to_degrees(value, centroid_lat)


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    gap_width: tuple[str, float | None],
    snap_tolerance: tuple[str, float | None],
    debug: bool = False,
) -> None:
    """Fix gap/overlap defects in `{name}_01`, writing `{name}_03`."""
    table = f"{name}_01"

    if not has_coverage_violations(conn, table):
        conn.execute(f'CREATE OR REPLACE TABLE "{name}_03" AS SELECT * FROM "{table}"')
        return

    centroid_lat = centroid_lat_of(conn, table)
    gap_max_width_deg = _resolve_gap_max_width_deg(conn, name, gap_width, centroid_lat)
    snap_mode, snap_value = snap_tolerance
    snap_distance_deg = (
        -1.0 if snap_mode == "auto" else meters_to_degrees(snap_value, centroid_lat)
    )

    try:
        coverage_clean(
            conn, table, f"{name}_03", None, gap_max_width_deg, snap_distance_deg
        )
    except Exception as e:  # noqa: BLE001 -- GEOS topology failures surface as generic duckdb errors
        logger.warning(
            "coverage_clean failed on %s (%s), retrying at reduced precision", table, e
        )
        reduced = f"{table}_reduced"
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{reduced}" AS
            SELECT * EXCLUDE (geom),
                   ST_ReducePrecision(geom, {REDUCED_PRECISION_DEG}) AS geom
            FROM "{table}"
        """)
        coverage_clean(
            conn, reduced, f"{name}_03", None, gap_max_width_deg, snap_distance_deg
        )
        if not debug:
            conn.execute(f'DROP TABLE IF EXISTS "{reduced}"')
