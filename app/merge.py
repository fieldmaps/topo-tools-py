from pathlib import Path

import duckdb

from .config import PARQUET_OPTS
from .utils import coverage_clean, parquet


def main(conn: duckdb.DuckDBPyConnection, name: str, *_: list) -> None:
    """Merge original geometryetry with extended Voronoi polygons."""
    p01 = parquet(f"{name}_01")
    p04 = parquet(f"{name}_04")
    p05_tmp1 = parquet(f"{name}_05_tmp1")
    p05_tmp2 = parquet(f"{name}_05_tmp2")
    p05_tmp3 = parquet(f"{name}_05_tmp3")
    p05 = parquet(f"{name}_05")

    # Union of all original polygons (single row)
    conn.execute(f"""--sql
        COPY (
            SELECT ST_Multi(ST_Union_Agg(geometry)) AS geometry
            FROM read_parquet('{p01}')
        ) TO '{p05_tmp1}' {PARQUET_OPTS}
    """)

    # Original polygons plus the Voronoi extension outside the original coverage
    conn.execute(f"""--sql
        COPY (
            SELECT fid, geometry FROM read_parquet('{p01}')
            UNION ALL
            SELECT
                a.fid,
                ST_Multi(ST_MakeValid(
                    ST_Difference(a.geometry, b.geometry)
                )) AS geometry
            FROM read_parquet('{p04}') AS a
            JOIN read_parquet('{p05_tmp1}') AS b
            ON ST_Intersects(a.geometry, b.geometry)
        ) TO '{p05_tmp2}' {PARQUET_OPTS}
    """)

    # Re-union by fid to merge original + extended parts
    conn.execute(f"""--sql
        COPY (
            SELECT fid, ST_Multi(ST_Union_Agg(geometry)) AS geometry
            FROM read_parquet('{p05_tmp2}')
            GROUP BY fid
        ) TO '{p05_tmp3}' {PARQUET_OPTS}
    """)

    # Coverage clean
    coverage_clean(p05_tmp3, p05)

    Path(p05_tmp1).unlink()
    Path(p05_tmp2).unlink()
    Path(p05_tmp3).unlink()
