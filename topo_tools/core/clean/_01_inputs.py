"""Imports geodata and reprojects to EPSG:4326, without auto-cleaning coverage.

Unlike extend's own inputs stage, this deliberately skips the automatic
ST_CoverageClean pre-check: clean's whole purpose is to detect defects in the
*raw* input, so _02_issues.py needs to see them, not a table that's already
been silently rewritten.
"""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.extend._01_inputs import read_and_reproject


def main(conn: DuckDBPyConnection, name: str, path: Path) -> None:
    """Read geodata into `{name}_01`, reprojected to EPSG:4326, uncleaned."""
    read_and_reproject(conn, name, path)
