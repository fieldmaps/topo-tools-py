from decimal import Decimal
from pathlib import Path

import duckdb

from .config import PARQUET_OPTS
from .utils import parquet


def main(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    __: Path,
    ___: str,
    distance: Decimal,
) -> None:
    """Create points along boundary lines."""
    p02 = parquet(f"{name}_02")
    p03_tmp1 = parquet(f"{name}_03_tmp1")
    p03 = parquet(f"{name}_03")
    d = float(distance)

    # Small buffer around all line endpoints to mark the shared-boundary zone
    conn.execute(f"""--sql
        COPY (
            SELECT ST_Multi(ST_Union_Agg(ST_Buffer(ST_Boundary(geometry), 0.00000001)))
                AS geometry
            FROM read_parquet('{p02}')
        ) TO '{p03_tmp1}' {PARQUET_OPTS}
    """)

    # Interpolated points along each line minus the shared-boundary zone,
    # union'd with the line endpoints also minus the shared-boundary zone
    conn.execute(f"""--sql
        COPY (
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
            FROM read_parquet('{p02}') AS a
            CROSS JOIN read_parquet('{p03_tmp1}') AS b
            UNION ALL
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Boundary(
                    ST_Difference(a.geometry, b.geometry)
                ))).geom AS geometry
            FROM read_parquet('{p02}') AS a
            CROSS JOIN read_parquet('{p03_tmp1}') AS b
        ) TO '{p03}' {PARQUET_OPTS}
    """)

    Path(p03_tmp1).unlink()
