"""Validates the cleaned output and exports the cleaned dataset + issues report."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.extend._constants import COPY_OPTS
from topo_tools.core.extend._coverage import check_overlaps

logger = getLogger(__name__)


def _warn_on_unfilled_gaps(conn: DuckDBPyConnection, name: str) -> None:
    """Log (never raise) how many detected gaps remain uncovered by `{name}_03`.

    Unlike extend/match, clean can legitimately leave gaps unfilled by design
    (--gap-width auto, or a numeric cap narrower than some detected gap) --
    this is visibility for the issues file, not a failure condition.
    """
    row = conn.execute(f"""--sql
        WITH u AS (SELECT ST_Union_Agg(geom) AS g FROM "{name}_03")
        SELECT
            COUNT(*) FILTER (WHERE NOT ST_Contains(u.g, ST_PointOnSurface(i.geom))),
            COUNT(*)
        FROM "{name}_02" i, u
        WHERE i.kind = 'gap'
    """).fetchall()[0]
    remaining, total = row
    if total and remaining:
        logger.warning(
            "clean: %d of %d detected gap(s) remain unfilled -- see the issues file",
            remaining,
            total,
        )


def main(
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    issues_dest: Path,
    *,
    debug: bool = False,
) -> None:
    """Validate `{name}_03` and export the cleaned dataset + issues report."""
    check_overlaps(conn, f"{name}_03")
    _warn_on_unfilled_gaps(conn, name)

    dest.parent.mkdir(exist_ok=True, parents=True)
    issues_dest.parent.mkdir(exist_ok=True, parents=True)

    conn.execute(f"""--sql
        COPY (
            SELECT * EXCLUDE (fid) RENAME (geom AS geometry)
            FROM "{name}_03"
        ) TO '{dest}' {COPY_OPTS[dest.suffix]}
    """)
    conn.execute(f"""--sql
        COPY (
            SELECT * RENAME (geom AS geometry)
            FROM "{name}_02"
        ) TO '{issues_dest}' {COPY_OPTS[issues_dest.suffix]}
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03"')
