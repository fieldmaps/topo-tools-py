from typing import LiteralString

from psycopg import Connection
from psycopg.sql import SQL, Identifier

from .topology import check_gaps, check_missing_rows, check_overlaps

query_1: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        (ST_Dump(
            ST_CollectionExtract(ST_MakeValid(
                ST_VoronoiPolygons(ST_Collect(geom))
            ), 3)
        )).geom::GEOMETRY(Polygon, 4326) AS geom
    FROM {table_in};
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
query_2: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        a.fid,
        b.geom
    FROM {table_in1} AS a
    JOIN {table_in2} AS b
    ON ST_Within(a.geom, b.geom);
"""
query_3: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        fid,
        ST_Multi(
            ST_Union(geom)
        )::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in}
    GROUP BY fid;
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
query_4: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        fid,
        ST_Multi(ST_MakeValid(
            ST_CoverageClean(geom) OVER ()
        ))::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in};
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
query_5: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out} CASCADE;
    CREATE TABLE {table_out} AS
    SELECT
        fid,
        ST_Multi(
            ST_CollectionExtract(ST_Intersection(
                a.geom,
                ST_MakeEnvelope(-180, -90, 180, 90, 4326)
            ), 3)
        )::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in} AS a;
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
drop_tmp: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_tmp1};
    DROP TABLE IF EXISTS {table_tmp2};
    DROP TABLE IF EXISTS {table_tmp3};
    DROP TABLE IF EXISTS {table_tmp4};
"""


def main(conn: Connection, name: str) -> None:
    """Create Voronoi polygons from points."""
    conn.execute(
        SQL(query_1).format(
            table_in=Identifier(f"{name}_03"),
            table_out=Identifier(f"{name}_04_tmp1"),
        ),
    )
    conn.execute(
        SQL(query_2).format(
            table_in1=Identifier(f"{name}_03"),
            table_in2=Identifier(f"{name}_04_tmp1"),
            table_out=Identifier(f"{name}_04_tmp2"),
        ),
    )
    check_missing_rows(conn, name, f"{name}_03", f"{name}_04_tmp2")
    conn.execute(
        SQL(query_3).format(
            table_in=Identifier(f"{name}_04_tmp2"),
            table_out=Identifier(f"{name}_04_tmp3"),
        ),
    )
    check_overlaps(conn, name, f"{name}_04_tmp3")
    conn.execute(
        SQL(query_4).format(
            table_in=Identifier(f"{name}_04_tmp3"),
            table_out=Identifier(f"{name}_04_tmp4"),
        ),
    )
    check_gaps(conn, name, f"{name}_04_tmp4")
    conn.execute(
        SQL(query_4).format(
            table_in=Identifier(f"{name}_04_tmp4"),
            table_out=Identifier(f"{name}_04"),
        ),
    )
    conn.execute(
        SQL(drop_tmp).format(
            table_tmp1=Identifier(f"{name}_04_tmp1"),
            table_tmp2=Identifier(f"{name}_04_tmp2"),
            table_tmp3=Identifier(f"{name}_04_tmp3"),
            table_tmp4=Identifier(f"{name}_04_tmp4"),
        ),
    )
