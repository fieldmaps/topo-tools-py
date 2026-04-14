from pathlib import Path

import duckdb

from .utils import _PARQUET_OPTS, parquet


def main(conn: duckdb.DuckDBPyConnection, name: str, *_: list) -> None:
    """Create boundary lines from polygons."""
    p01 = parquet(f"{name}_01")
    p02_tmp1 = parquet(f"{name}_02_tmp1")
    p02_tmp2 = parquet(f"{name}_02_tmp2")
    p02_tmp3 = parquet(f"{name}_02_tmp3")
    p02 = parquet(f"{name}_02")

    # Per-polygon boundary lines
    conn.execute(f"""
        COPY (
            SELECT fid, ST_Multi(ST_Boundary(geom)) AS geom
            FROM read_parquet('{p01}')
        ) TO '{p02_tmp1}' {_PARQUET_OPTS}
    """)

    # Union of all boundaries (single row)
    conn.execute(f"""
        COPY (
            SELECT ST_Multi(ST_Boundary(ST_Union_Agg(geom))) AS geom
            FROM read_parquet('{p01}')
        ) TO '{p02_tmp2}' {_PARQUET_OPTS}
    """)

    # Intersect per-polygon boundaries with the total union boundary
    conn.execute(f"""
        COPY (
            SELECT
                a.fid,
                ST_Multi(ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 2))
                    AS geom
            FROM read_parquet('{p02_tmp1}') AS a
            JOIN read_parquet('{p02_tmp2}') AS b
            ON ST_Intersects(a.geom, b.geom)
        ) TO '{p02_tmp3}' {_PARQUET_OPTS}
    """)

    # Merge lines per polygon and dump into individual LineStrings
    conn.execute(f"""
        COPY (
            SELECT fid, UNNEST(ST_Dump(ST_LineMerge(geom))).geom AS geom
            FROM read_parquet('{p02_tmp3}')
        ) TO '{p02}' {_PARQUET_OPTS}
    """)

    Path(p02_tmp1).unlink()
    Path(p02_tmp2).unlink()
    Path(p02_tmp3).unlink()
