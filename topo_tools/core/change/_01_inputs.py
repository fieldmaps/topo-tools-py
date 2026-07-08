"""Loads and cleans the old (Version A) and new (Version B) layers."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.extend import _01_inputs as extend_inputs


def main(conn: DuckDBPyConnection, name: str, old_path: Path, new_path: Path) -> None:
    """Load and coverage-clean both comparison layers.

    Reuses extend's full loader (with its auto-clean check) for both sides --
    unlike clean, change isn't trying to detect defects in the raw input,
    it's comparing two whole layers, so pre-cleaning each side reduces the
    risk of native GEOS choking on invalid geometry during ST_Intersection,
    with no downside. Mirrors match/_01_inputs.py's identical reasoning.
    """
    extend_inputs.main(conn, f"{name}_a", old_path)
    extend_inputs.main(conn, f"{name}_b", new_path)
