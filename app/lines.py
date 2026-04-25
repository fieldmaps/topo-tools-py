"""Extracts polygon boundary lines and retains per-polygon attributes."""

from duckdb import DuckDBPyConnection


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Create boundary lines from polygons."""
    # Per-polygon boundary lines
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        SELECT fid, ST_Multi(ST_Boundary(geom)) AS geom
        FROM "{name}_01"
    """)

    # Exterior edges = each polygon's boundary minus the union of touching
    # neighbours' boundaries. The lateral join finds neighbours locally so
    # there is no global ST_Union_Agg over all polygons in the dataset.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT
            a.fid,
            UNNEST(ST_Dump(ST_LineMerge(ST_CollectionExtract(
                CASE WHEN sub.neighbor_union IS NOT NULL
                    THEN ST_Difference(a.geom, sub.neighbor_union)
                    ELSE a.geom
                END, 2
            )))).geom AS geom
        FROM "{name}_02_tmp1" AS a
        LEFT JOIN LATERAL (
            SELECT ST_Union_Agg(b.geom) AS neighbor_union
            FROM "{name}_02_tmp1" AS b
            WHERE b.fid != a.fid AND ST_Intersects(a.geom, b.geom)
        ) AS sub ON true
    """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp1"')
