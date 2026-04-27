"""Creates interpolated points along boundary lines at configurable intervals."""

from decimal import Decimal

from duckdb import DuckDBPyConnection

from app.config import SNAP_TOLERANCE


def main(conn: DuckDBPyConnection, name: str, distance: Decimal) -> None:
    """Create points along boundary lines."""
    d = float(distance)

    # Small buffer around all line endpoints to mark the shared-boundary zone
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp1" AS
        SELECT ST_Union_Agg(ST_Buffer(ST_Boundary(geom), {SNAP_TOLERANCE}))
            AS geom
        FROM "{name}_02"
    """)

    # Interpolated points along each line minus the shared-boundary zone,
    # union'd with the line endpoints also minus the shared-boundary zone
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03" AS
        SELECT fid, geom FROM (
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Difference(
                    ST_LineInterpolatePoints(
                        a.geom,
                        LEAST({d!r} / ST_Length(a.geom), 1.0),
                        true
                    ),
                    b.geom
                ))).geom AS geom
            FROM "{name}_02" AS a
            CROSS JOIN "{name}_03_tmp1" AS b
            UNION ALL
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Boundary(
                    ST_Difference(a.geom, b.geom)
                ))).geom AS geom
            FROM "{name}_02" AS a
            CROSS JOIN "{name}_03_tmp1" AS b
        )
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp1"')
