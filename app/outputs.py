"""Exports output files from the final merged geometry table."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from .config import COPY_OPTS, check, debug, output_dir, output_file
from .topology import check_gaps, check_missing_rows, check_overlaps

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Output results to path."""
    checks = [
        lambda: check_missing_rows(conn, f"{name}_05", f"{name}_01"),
    ]
    if check:
        checks = [
            lambda: check_overlaps(conn, f"{name}_05"),
            lambda: check_gaps(conn, f"{name}_05"),
            *checks,
        ]

    for run_check in checks:
        try:
            run_check()
        except RuntimeError as e:
            logger.warning(e)

    dest = output_file or output_dir / path.name
    dest.parent.mkdir(exist_ok=True, parents=True)

    conn.execute(f"""--sql
        COPY (
            SELECT * EXCLUDE (fid) RENAME (geom AS geometry)
            FROM "{name}_05"
        ) TO '{dest}' {COPY_OPTS[path.suffix]}
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
