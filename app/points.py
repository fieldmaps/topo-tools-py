"""Creates interpolated points along boundary lines at configurable intervals."""

from decimal import Decimal
from pathlib import Path

import duckdb


def main(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    __: Path,
    ___: str,
    distance: Decimal,
) -> None:
    """Create points along boundary lines."""
    d = float(distance)

    # Small buffer around all line endpoints to mark the shared-boundary zone
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp1" AS
        SELECT ST_Multi(ST_Union_Agg(ST_Buffer(ST_Boundary(geometry), 0.00000001)))
            AS geometry
        FROM "{name}_02"
    """)

    # Interpolated points along each line minus the shared-boundary zone,
    # union'd with the line endpoints also minus the shared-boundary zone
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03" AS
        SELECT fid, geometry FROM (
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Difference(
                    ST_LineInterpolatePoints(
                        a.geometry,
                        LEAST({d!r} / ST_Length(a.geometry), 1.0),
                        true
                    ),
                    b.geometry
                ))).geom AS geometry
            FROM "{name}_02" AS a
            CROSS JOIN "{name}_03_tmp1" AS b
            UNION ALL
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Boundary(
                    ST_Difference(a.geometry, b.geometry)
                ))).geom AS geometry
            FROM "{name}_02" AS a
            CROSS JOIN "{name}_03_tmp1" AS b
        )
        WHERE geometry IS NOT NULL AND NOT ST_IsEmpty(geometry)
    """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp1"')
