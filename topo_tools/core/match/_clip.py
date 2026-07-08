"""Clips extended geometry to a single parent/clip polygon."""

from pathlib import Path

from duckdb import DuckDBPyConnection


def clip_to_parent_geom(
    conn: DuckDBPyConnection,
    table_in: str,
    parent_parquet_path: Path,
    table_out: str,
) -> None:
    """Clip every row of table_in to a parent polygon loaded from a parquet file.

    Used by the group worker subprocess, which only has the parent's exported
    geometry file, not a live parent table to filter by fid.
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        SELECT t.* EXCLUDE (geom),
               ST_Intersection(t.geom, p.geom) AS geom
        FROM "{table_in}" t, (
            SELECT geom FROM read_parquet('{parent_parquet_path}')
        ) p
        WHERE NOT ST_IsEmpty(ST_Intersection(t.geom, p.geom))
    """)
