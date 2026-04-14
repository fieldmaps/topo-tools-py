from pathlib import Path

import duckdb

from .config import PARQUET_OPTS
from .utils import parquet


def main(conn: duckdb.DuckDBPyConnection, name: str, *_: list) -> None:
    """Create boundary lines from polygons."""
    p01 = parquet(f"{name}_01")
    p02_tmp1 = parquet(f"{name}_02_tmp1")
    p02_tmp2 = parquet(f"{name}_02_tmp2")
    p02_tmp3 = parquet(f"{name}_02_tmp3")
    p02 = parquet(f"{name}_02")

    # Per-polygon boundary lines
    conn.execute(f"""--sql
        COPY (
            SELECT fid, ST_Multi(ST_Boundary(geometry)) AS geometry
            FROM read_parquet('{p01}')
        ) TO '{p02_tmp1}' {PARQUET_OPTS}
    """)

    # Union of all boundaries (single row)
    conn.execute(f"""--sql
        COPY (
            SELECT ST_Multi(ST_Boundary(ST_Union_Agg(geometry))) AS geometry
            FROM read_parquet('{p01}')
        ) TO '{p02_tmp2}' {PARQUET_OPTS}
    """)

    # Intersect per-polygon boundaries with the total union boundary
    conn.execute(f"""--sql
        COPY (
            SELECT
                a.fid,
                ST_Multi(ST_CollectionExtract(
                    ST_Intersection(a.geometry, b.geometry), 2
                )) AS geometry
            FROM read_parquet('{p02_tmp1}') AS a
            JOIN read_parquet('{p02_tmp2}') AS b
            ON ST_Intersects(a.geometry, b.geometry)
        ) TO '{p02_tmp3}' {PARQUET_OPTS}
    """)

    # Merge lines per polygon and dump into individual LineStrings
    conn.execute(f"""--sql
        COPY (
            SELECT fid, UNNEST(ST_Dump(ST_LineMerge(geometry))).geom AS geometry
            FROM read_parquet('{p02_tmp3}')
        ) TO '{p02}' {PARQUET_OPTS}
    """)

    Path(p02_tmp1).unlink()
    Path(p02_tmp2).unlink()
    Path(p02_tmp3).unlink()
