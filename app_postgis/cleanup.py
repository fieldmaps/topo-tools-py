from typing import LiteralString

from psycopg import Connection
from psycopg.sql import SQL, Identifier

drop_tmp: LiteralString = """--sql
    DROP VIEW IF EXISTS {view_06};
    DROP TABLE IF EXISTS {table_attr};
    DROP TABLE IF EXISTS {table_01};
    DROP TABLE IF EXISTS {table_02};
    DROP TABLE IF EXISTS {table_03};
    DROP TABLE IF EXISTS {table_04};
    DROP TABLE IF EXISTS {table_05};
"""


def main(conn: Connection, name: str, *_: list) -> None:
    """Drop temporary tables."""
    conn.execute(
        SQL(drop_tmp).format(
            table_attr=Identifier(f"{name}_attr"),
            table_01=Identifier(f"{name}_01"),
            table_02=Identifier(f"{name}_02"),
            table_03=Identifier(f"{name}_03"),
            table_04=Identifier(f"{name}_04"),
            table_05=Identifier(f"{name}_05"),
            view_06=Identifier(f"{name}_06"),
        ),
    )
