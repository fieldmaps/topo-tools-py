from pathlib import Path

import duckdb

from .topology import check_gaps, check_missing_rows, check_overlaps
from .utils import _PARQUET_OPTS, coverage_clean, parquet


def main(conn: duckdb.DuckDBPyConnection, name: str, *_: list) -> None:
    """Create Voronoi polygons from points."""
    p03 = parquet(f"{name}_03")
    p04_tmp1 = parquet(f"{name}_04_tmp1")
    p04_tmp2 = parquet(f"{name}_04_tmp2")
    p04_tmp3 = parquet(f"{name}_04_tmp3")
    p04_tmp4 = parquet(f"{name}_04_tmp4")
    p04 = parquet(f"{name}_04")

    # Voronoi diagram from all input points
    conn.execute(f"""
        COPY (
            SELECT UNNEST(ST_Dump(
                ST_CollectionExtract(ST_MakeValid(
                    ST_VoronoiDiagram(ST_Collect(list(geom)))
                ), 3)
            )).geom AS geom
            FROM read_parquet('{p03}')
        ) TO '{p04_tmp1}' {_PARQUET_OPTS}
    """)

    # Assign source fid to each Voronoi cell via point-in-polygon
    conn.execute(f"""
        COPY (
            SELECT a.fid, b.geom
            FROM read_parquet('{p03}') AS a
            JOIN read_parquet('{p04_tmp1}') AS b
            ON ST_Within(a.geom, b.geom)
        ) TO '{p04_tmp2}' {_PARQUET_OPTS}
    """)
    check_missing_rows(conn, name, p03, p04_tmp2)

    # Union Voronoi cells by fid
    conn.execute(f"""
        COPY (
            SELECT fid, ST_Multi(ST_Union_Agg(geom)) AS geom
            FROM read_parquet('{p04_tmp2}')
            GROUP BY fid
        ) TO '{p04_tmp3}' {_PARQUET_OPTS}
    """)
    check_overlaps(conn, name, p04_tmp3)

    # Coverage clean pass 1
    coverage_clean(p04_tmp3, p04_tmp4)
    check_gaps(conn, name, p04_tmp4)

    # Coverage clean pass 2
    coverage_clean(p04_tmp4, p04)

    Path(p04_tmp1).unlink()
    Path(p04_tmp2).unlink()
    Path(p04_tmp3).unlink()
    Path(p04_tmp4).unlink()
