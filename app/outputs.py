"""Joins geometry with original attributes and exports output files."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from .config import COPY_OPTS, output_dir, output_file
from .topology import check_gaps, check_missing_rows, check_overlaps

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Output results to path."""
    for check in (
        lambda: check_overlaps(conn, f"{name}_05"),
        lambda: check_gaps(conn, f"{name}_05"),
        lambda: check_missing_rows(conn, f"{name}_05", f"{name}_attr"),
    ):
        try:
            check()
        except RuntimeError as e:
            logger.warning(e)

    dest = output_file or output_dir / path.name
    dest.parent.mkdir(exist_ok=True, parents=True)

    conn.execute(f"""--sql
        COPY (
            SELECT a.geom AS geometry, b.* EXCLUDE (fid)
            FROM "{name}_05" AS a
            LEFT JOIN "{name}_attr" AS b
            ON a.fid = b.fid
        ) TO '{dest}' {COPY_OPTS[path.suffix]}
    """)
