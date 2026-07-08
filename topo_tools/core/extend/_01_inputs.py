"""Imports geodata, reprojects to EPSG:4326, and cleans coverage violations."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from ._constants import RESERVED_COLUMN_NAMES
from ._coverage import coverage_clean, has_coverage_violations

logger = getLogger(__name__)


def read_and_reproject(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Read geodata and reproject to EPSG:4326, storing the canonical `{name}_01` table.

    Split out from `main()` so `core.clean` can read+reproject without the
    auto-clean pass below -- clean's detection stage needs to see the *raw*
    input, not one ST_CoverageClean has already silently rewritten.
    """
    read_expr = (
        f"SELECT * FROM '{path}'"
        if path.suffix == ".parquet"
        else f"SELECT * FROM ST_Read('{path}')"
    )

    schema = conn.execute(f"DESCRIBE {read_expr}").fetchall()
    geom_col, geom_type = next(
        (col[0], col[1]) for col in schema if col[1].startswith("GEOMETRY")
    )
    # A source column already named "fid"/"OGC_FID" would otherwise collide
    # with our own row_number() AS fid below (duplicate column) or with
    # GDAL's reserved FID handling on export (see RESERVED_COLUMN_NAMES) --
    # rename it once here so nothing downstream has to guard against it.
    colliding_cols = [col[0] for col in schema if col[0] in RESERVED_COLUMN_NAMES]
    if colliding_cols:
        logger.warning(
            "renaming source column(s) %s to %s to avoid colliding with "
            "topo-tools' internal fid / GDAL's reserved OGC_FID field",
            colliding_cols,
            [f"{c}_orig" for c in colliding_cols],
        )
    exclude_cols = [
        col[0]
        for col in schema
        if col[1].startswith("GEOMETRY")
        or (col[0].endswith("_bbox") and col[1].startswith("STRUCT"))
    ] + colliding_cols
    exclude_sql = ", ".join(f'"{c}"' for c in exclude_cols)
    rename_sql = "".join(f', "{c}" AS "{c}_orig"' for c in colliding_cols)

    # ST_Read tags geometry with source CRS; single-arg ST_Transform infers it.
    # Parquet geometries are untagged (assumed EPSG:4326), so skip transform.
    geom_expr = (
        f"ST_Force2D(ST_Transform(ST_MakeValid(\"{geom_col}\"), 'EPSG:4326'))"
        if geom_type != "GEOMETRY"
        else f'ST_Force2D(ST_MakeValid("{geom_col}"))'
    )

    # Reproject to EPSG:4326 and store as the canonical input table. ST_MakeValid
    # repairs broken ring orientations or self-intersections before transform.
    # ST_Force2D drops any Z/M coordinates that downstream GEOS operations
    # don't handle correctly. Parquet inputs skip ST_Transform (already WGS84).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01" AS
        SELECT * EXCLUDE ({exclude_sql}){rename_sql},
               row_number() OVER () AS fid,
               {geom_expr} AS geom
        FROM ({read_expr})
    """)


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Import geodata into DuckDB tables, then clean coverage topology violations.

    ST_CoverageInvalidEdges_Agg gates whether ST_CoverageClean runs at all —
    no-op when the input coverage has no invalid edges. Otherwise every
    polygon's coordinates may shift, not just the violating ones. Does not
    distinguish real holes from digitization slivers: inputs are expected to
    be pre-cleaned upstream, and any narrow gap that slips through is treated
    the same as a real hole (lake, enclave) — both are legitimate work for
    the Voronoi-extension stage to divide across bordering polygons.
    """
    read_and_reproject(conn, name, path)

    if has_coverage_violations(conn, f"{name}_01"):
        logger.info("cleaning coverage: invalid edges detected")
        coverage_clean(conn, f"{name}_01", f"{name}_01", None, None)
