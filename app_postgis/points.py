from decimal import Decimal
from pathlib import Path
from typing import LiteralString

from psycopg import Connection
from psycopg.sql import SQL, Identifier, Literal

query_1: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        ST_Multi(ST_Union(
            ST_Buffer(ST_Boundary(geom), 0.00000001)
        ))::GEOMETRY(MultiPolygon, 4326) AS geom
    FROM {table_in};
"""
query_2: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_out};
    CREATE TABLE {table_out} AS
    SELECT
        a.fid,
        (ST_Dump(
            ST_Difference(
                ST_LineInterpolatePoints(
                    a.geom,
                    LEAST({distance}/ST_Length(a.geom), 1)
                ),
                b.geom
            )
        )).geom::GEOMETRY(Point, 4326) AS geom
    FROM {table_in1} AS a
    CROSS JOIN {table_in2} AS b
    UNION ALL
    SELECT
        a.fid,
        (ST_Dump(
            ST_Boundary(ST_Difference(a.geom, b.geom))
        )).geom::GEOMETRY(Point, 4326) AS geom
    FROM {table_in1} AS a
    CROSS JOIN {table_in2} AS b;
    CREATE INDEX ON {table_out} USING GIST(geom);
"""
drop_tmp: LiteralString = """--sql
    DROP TABLE IF EXISTS {table_tmp1};
    DROP TABLE IF EXISTS {table_tmp2};
"""


def main(conn: Connection, name: str, __: Path, ___: str, distance: Decimal) -> None:
    """Create points along boundary lines."""
    conn.execute(
        SQL(query_1).format(
            table_in=Identifier(f"{name}_02"),
            table_out=Identifier(f"{name}_03_tmp1"),
        ),
    )
    conn.execute(
        SQL(query_2).format(
            table_in1=Identifier(f"{name}_02"),
            table_in2=Identifier(f"{name}_03_tmp1"),
            distance=Literal(distance),
            table_out=Identifier(f"{name}_03"),
        ),
    )
    conn.execute(
        SQL(drop_tmp).format(
            table_tmp1=Identifier(f"{name}_03_tmp1"),
            table_tmp2=Identifier(f"{name}_03_tmp2"),
        ),
    )
