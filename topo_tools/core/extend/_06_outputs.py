"""Validates topology and exports output files from the final merged geometry table."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from ._constants import COPY_OPTS
from ._coverage import check_gaps, check_overlaps


def main(
    conn: DuckDBPyConnection, name: str, dest: Path, *, debug: bool = False
) -> None:
    """Output results to dest."""
    check_overlaps(conn, f"{name}_05")
    check_gaps(conn, f"{name}_05")

    dest.parent.mkdir(exist_ok=True, parents=True)

    conn.execute(f"""--sql
        COPY (
            SELECT * EXCLUDE (fid) RENAME (geom AS geometry)
            FROM "{name}_05"
        ) TO '{dest}' {COPY_OPTS[dest.suffix]}
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05_tmp3"')
