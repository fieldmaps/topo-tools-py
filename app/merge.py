"""Unions Voronoi extensions with original polygons."""

from duckdb import DuckDBPyConnection


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Merge original geom with extended Voronoi polygons."""
    # Node ALL boundaries together: original polygon boundaries and all Voronoi cell
    # boundaries. This ensures every crossing point (where a Voronoi edge meets an
    # original polygon edge) becomes a shared vertex in both geometries, so adjacent
    # merged polygons always have consistent edge structure — no topology seams.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_noded" AS
        WITH orig_bd AS (
            SELECT ST_Union_Agg(ST_Boundary(geom)) AS geom FROM "{name}_01"
        ),
        voro_bd AS (
            SELECT ST_Union_Agg(ST_Boundary(geom)) AS geom FROM "{name}_04"
        )
        SELECT ST_Node(ST_Collect(list(geom))) AS geom FROM (
            SELECT geom FROM orig_bd
            UNION ALL
            SELECT geom FROM voro_bd
        )
    """)

    # Polygonize the noded edge network into all coverage pieces.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_pieces" AS
        SELECT UNNEST(ST_Dump(ST_Polygonize(list(geom)))).geom AS geom
        FROM "{name}_05_noded"
    """)

    # Assign each piece to a fid. Original polygon assignment takes priority so the
    # original coverage is preserved exactly, even when Voronoi cells extend through it.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05_assigned" AS
        SELECT COALESCE(o.fid, v.fid) AS fid, p.geom
        FROM "{name}_05_pieces" AS p
        LEFT JOIN "{name}_01" AS o ON ST_Within(ST_PointOnSurface(p.geom), o.geom)
        LEFT JOIN "{name}_04" AS v ON ST_Within(ST_PointOnSurface(p.geom), v.geom)
        WHERE COALESCE(o.fid, v.fid) IS NOT NULL
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        SELECT fid, ST_Multi(ST_Union_Agg(geom)) AS geom
        FROM "{name}_05_assigned"
        GROUP BY fid
    """)

    for tmp in ["05_noded", "05_pieces", "05_assigned"]:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_{tmp}"')
