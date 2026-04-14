from typing import LiteralString

from psycopg import Connection
from psycopg.sql import SQL, Identifier

query_1: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        ST_Multi(
            ST_Union(geom)
        )::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in};
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
query_2: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT fid, geom
    FROM {table_in1}
    UNION ALL
    SELECT
        a.fid,
        ST_Multi(ST_MakeValid(
            ST_Difference(a.geom, b.geom)
        ))::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in2} AS a
    JOIN {table_in3} AS b
    ON ST_Intersects(a.geom, b.geom);
"""
query_3: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out} CASCADE;
    CREATE TABLE {table_out} AS
    SELECT
        fid,
        ST_Multi(ST_Union(
            geom
        ))::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in}
    GROUP BY fid;
"""
query_4: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out} CASCADE;
    CREATE TABLE {table_out} AS
    SELECT
        fid,
        ST_Multi(ST_MakeValid(
            ST_CoverageClean(geom) OVER ()
        ))::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in};
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
drop_tmp: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_tmp1};
    DROP TABLE IF EXISTS {table_tmp2};
    DROP TABLE IF EXISTS {table_tmp3};
"""


def main(conn: Connection, name: str, *_: list) -> None:
    """Merge original geometry with extended polygons."""
    conn.execute(
        SQL(query_1).format(
            table_in=Identifier(f"{name}_01"),
            table_out=Identifier(f"{name}_05_tmp1"),
        ),
    )
    conn.execute(
        SQL(query_2).format(
            table_in1=Identifier(f"{name}_01"),
            table_in2=Identifier(f"{name}_04"),
            table_in3=Identifier(f"{name}_05_tmp1"),
            table_out=Identifier(f"{name}_05_tmp2"),
        ),
    )
    conn.execute(
        SQL(query_3).format(
            table_in=Identifier(f"{name}_05_tmp2"),
            table_out=Identifier(f"{name}_05_tmp3"),
        ),
    )
    conn.execute(
        SQL(query_4).format(
            table_in=Identifier(f"{name}_05_tmp3"),
            table_out=Identifier(f"{name}_05"),
        ),
    )
    conn.execute(
        SQL(drop_tmp).format(
            table_tmp1=Identifier(f"{name}_05_tmp1"),
            table_tmp2=Identifier(f"{name}_05_tmp2"),
            table_tmp3=Identifier(f"{name}_05_tmp3"),
        ),
    )
