"""Single whole-table coverage-clean of the reassembled matched output."""

from duckdb import DuckDBPyConnection

from topo_tools.core.extend._constants import SNAP_TOLERANCE
from topo_tools.core.extend._coverage import coverage_clean


def main(conn: DuckDBPyConnection, name: str, *, debug: bool = False) -> None:
    """Coverage-clean the reassembled output to fix cross-group seams.

    By construction every point of the reassembled extent belongs to exactly
    one surviving child fid, so anything ST_CoverageClean finds to close here
    is seam noise (float-precision mismatches at group-to-group boundaries and
    each group's own clip line), not a real feature to protect. fids stays
    None (whole-table): per-fid violator scoping was deliberately removed once
    already from extend's own merge stage because it reintroduced seam-gap
    bugs -- see docs/topology.md. Do not reintroduce it here.
    """
    coverage_clean(conn, f"{name}_03", f"{name}_04", None, SNAP_TOLERANCE)
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03"')
