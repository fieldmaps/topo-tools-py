from typing import LiteralString

from psycopg import Connection
from psycopg.sql import SQL, Identifier

query_1: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        fid,
        ST_Multi(
            ST_Boundary(geom)
        )::GEOMETRY(MultiLineString, 4326) AS geom
    FROM {table_in};
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
query_2: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        ST_Multi(
            ST_Boundary(ST_Union(geom))
        )::GEOMETRY(MultiLineString, 4326) AS geom
    FROM {table_in};
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
query_3: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        a.fid,
        ST_Multi(
            ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 2)
        )::GEOMETRY(MultiLineString, 4326) AS geom
    FROM {table_in1} AS a
    JOIN {table_in2} AS b
    ON ST_Intersects(a.geom, b.geom);
"""
query_4: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        fid,
        (ST_Dump(
            ST_LineMerge(geom))
        ).geom::GEOMETRY(LineString, 4326) AS geom
    FROM {table_in};
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
drop_tmp: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_tmp1};
    DROP TABLE IF EXISTS {table_tmp2};
    DROP TABLE IF EXISTS {table_tmp3};
"""


def main(conn: Connection, name: str, *_: list) -> None:
    """Create boundary lines from polygons."""
    conn.execute(
        SQL(query_1).format(
            table_in=Identifier(f"{name}_01"),
            table_out=Identifier(f"{name}_02_tmp1"),
        ),
    )
    conn.execute(
        SQL(query_2).format(
            table_in=Identifier(f"{name}_01"),
            table_out=Identifier(f"{name}_02_tmp2"),
        ),
    )
    conn.execute(
        SQL(query_3).format(
            table_in1=Identifier(f"{name}_02_tmp1"),
            table_in2=Identifier(f"{name}_02_tmp2"),
            table_out=Identifier(f"{name}_02_tmp3"),
        ),
    )
    conn.execute(
        SQL(query_4).format(
            table_in=Identifier(f"{name}_02_tmp3"),
            table_out=Identifier(f"{name}_02"),
        ),
    )
    conn.execute(
        SQL(drop_tmp).format(
            table_tmp1=Identifier(f"{name}_02_tmp1"),
            table_tmp2=Identifier(f"{name}_02_tmp2"),
            table_tmp3=Identifier(f"{name}_02_tmp3"),
        ),
    )
