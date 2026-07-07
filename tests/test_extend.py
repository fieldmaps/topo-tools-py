"""Portability smoke tests: does extend() run to completion on this machine.

Not a topology/correctness suite -- outputs.main already raises RuntimeError
on coverage violations, so a run that completes without raising has already
been vetted for correctness by the pipeline itself.
"""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.extend import extend
from topo_tools.cli.main import cli

# fid 4 is a MULTIPOLYGON (two disjoint parts) to exercise multipolygon
# handling; fid 1/2 touch exactly (shared edge, valid coverage); fid 3 sits
# across a deliberate gap from 1/2 for the Voronoi extension to fill.
_SYNTHETIC_WKT = [
    (1, "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
    (2, "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))"),
    (3, "POLYGON((3 0, 4 0, 4 1, 3 1, 3 0))"),
    (
        4,
        "MULTIPOLYGON(((0 3, 0.5 3, 0.5 3.5, 0 3.5, 0 3)), "
        "((1 3, 1.5 3, 1.5 3.5, 1 3.5, 1 3)))",
    ),
]

_STEPS = ["inputs", "lines", "attempt", "merge", "outputs"]


@pytest.fixture
def synthetic_input(tmp_path):
    """Write a small synthetic GeoParquet -- no real-world fixture needed."""
    path = tmp_path / "synthetic.parquet"
    values = ", ".join(
        f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in _SYNTHETIC_WKT
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")
    return path


def test_cli_help():
    result = CliRunner().invoke(cli, ["extend", "--help"])
    assert result.exit_code == 0
    assert "Extend polygon boundaries" in result.output


def test_extend_full_run(synthetic_input, tmp_path):
    output_path = tmp_path / "out.parquet"
    extend(synthetic_input, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        row_count = conn.execute(f"SELECT COUNT(*) FROM '{output_path}'").fetchone()[0]
    assert row_count == len(_SYNTHETIC_WKT)


def test_extend_steps(synthetic_input, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        extend(
            synthetic_input, output_path, tmp_dir=work_dir, step=step, overwrite=True
        )
    assert output_path.exists()
