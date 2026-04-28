"""Extracts polygon boundary lines and retains per-polygon attributes."""

from duckdb import DuckDBPyConnection


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Create boundary lines from polygons."""
    # Per-polygon boundary lines
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        SELECT fid, ST_Boundary(geom) AS geom
        FROM "{name}_01"
    """)

    # Exterior edges = each polygon's boundary minus the union of touching
    # neighbours' boundaries. The lateral join finds neighbours locally so
    # there is no global ST_Union_Agg over all polygons in the dataset.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02a" AS
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

    # Interior edges = each polygon's boundary intersected with its neighbours'
    # boundaries. Each shared edge appears once per adjacent polygon (double-
    # counted), which is fine for ST_Node in merge.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02b" AS
        SELECT UNNEST(ST_Dump(ST_LineMerge(geom))).geom AS geom
        FROM (
            SELECT ST_CollectionExtract(
                ST_Intersection(a.geom, sub.neighbor_union), 2
            ) AS geom
            FROM "{name}_02_tmp1" AS a
            LEFT JOIN LATERAL (
                SELECT ST_Union_Agg(b.geom) AS neighbor_union
                FROM "{name}_02_tmp1" AS b
                WHERE b.fid != a.fid AND ST_Intersects(a.geom, b.geom)
            ) AS sub ON true
            WHERE sub.neighbor_union IS NOT NULL
        )
        WHERE NOT ST_IsEmpty(geom)
    """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp1"')
