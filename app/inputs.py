"""Imports geodata via DuckDB spatial, reprojects to EPSG:4326, and stores as tables."""

from pathlib import Path

from duckdb import DuckDBPyConnection


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Import geodata into DuckDB tables."""
    read_expr = (
        f"SELECT * FROM '{path}'"
        if path.suffix == ".parquet"
        else f"SELECT * FROM ST_Read('{path}')"
    )

    schema = conn.execute(f"DESCRIBE {read_expr}").fetchall()
    geom_col, geom_type = next(
        (col[0], col[1]) for col in schema if col[1].startswith("GEOMETRY")
    )
    exclude_cols = [
        col[0]
        for col in schema
        if col[1].startswith("GEOMETRY")
        or (col[0].endswith("_bbox") and col[1].startswith("STRUCT"))
    ]
    exclude_sql = ", ".join(f'"{c}"' for c in exclude_cols)

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
        SELECT * EXCLUDE ({exclude_sql}),
               row_number() OVER () AS fid,
               {geom_expr} AS geom
        FROM ({read_expr})
    """)
