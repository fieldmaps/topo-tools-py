"""Portability smoke tests: does match() run to completion on this machine.

Not a topology/correctness suite -- outputs.main already raises RuntimeError
on coverage violations, so a run that completes without raising has already
been vetted for correctness by the pipeline itself.
"""

import logging

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.match import match
from topo_tools.cli.main import cli

# Parent A (large square) contains children 1 & 2 with a gap between them --
# exercises multi-child grouping, within-group Voronoi fill, and clip-to-
# parent. Parent B (disjoint large square) contains only child 3 alone --
# exercises the "always group, even size 1" path. Child 4 sits far outside
# both parents -- exercises the drop-unmatched-with-a-warning path.
_CHILD_WKT = [
    (1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))"),
    (2, "POLYGON((1.5 0.5, 2 0.5, 2 1, 1.5 1, 1.5 0.5))"),
    (3, "POLYGON((11 1, 12 1, 12 2, 11 2, 11 1))"),
    (4, "POLYGON((20 0, 21 0, 21 1, 20 1, 20 0))"),
]
_PARENT_WKT = [
    (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))"),
    (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))"),
]

_STEPS = ["inputs", "assign", "groups", "merge", "outputs"]


def _write_synthetic(path, wkt_rows):
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in wkt_rows)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")


@pytest.fixture
def synthetic_children(tmp_path):
    """Write a small synthetic child-layer GeoParquet -- no real-world fixture."""
    path = tmp_path / "children.parquet"
    _write_synthetic(path, _CHILD_WKT)
    return path


@pytest.fixture
def synthetic_parents(tmp_path):
    """Write a small synthetic parent/clip-layer GeoParquet."""
    path = tmp_path / "parents.parquet"
    _write_synthetic(path, _PARENT_WKT)
    return path


def test_cli_help():
    result = CliRunner().invoke(cli, ["match", "--help"])
    assert result.exit_code == 0
    assert "Match children to parents" in result.output
    assert "Examples:" in result.output


def test_match_full_run(synthetic_children, synthetic_parents, tmp_path):
    output_path = tmp_path / "out.parquet"
    match(synthetic_children, synthetic_parents, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
    assert ids == [1, 2, 3]


def test_match_drops_unassigned_and_warns(
    synthetic_children, synthetic_parents, tmp_path, caplog
):
    output_path = tmp_path / "out.parquet"
    with caplog.at_level(logging.WARNING):
        match(synthetic_children, synthetic_parents, output_path, overwrite=True)

    assert any("dropping" in r.message and "4" in r.message for r in caplog.records)


def test_match_single_parent_group(tmp_path):
    """Parent B has exactly one assigned child.

    Exercises the always-group, even-size-1 path explicitly, isolated from
    Parent A's multi-child group.
    """
    children_path = tmp_path / "children_single.parquet"
    parents_path = tmp_path / "parents_single.parquet"
    _write_synthetic(children_path, [_CHILD_WKT[2]])  # fid 3 only
    _write_synthetic(parents_path, [_PARENT_WKT[1]])  # Parent B only

    output_path = tmp_path / "out.parquet"
    match(children_path, parents_path, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        row_count = conn.execute(f"SELECT COUNT(*) FROM '{output_path}'").fetchone()[0]
    assert row_count == 1


def test_match_default_output_path(synthetic_children, synthetic_parents):
    match(synthetic_children, synthetic_parents, overwrite=True)

    expected = synthetic_children.with_stem(synthetic_children.stem + "_matched")
    assert expected.exists()


def test_cli_positional_args(synthetic_children, synthetic_parents, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli,
        ["match", str(synthetic_children), str(synthetic_parents), str(output_path)],
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_clip_file_required(synthetic_children):
    result = CliRunner().invoke(cli, ["match", str(synthetic_children)])
    assert result.exit_code != 0
    assert "Missing argument" in result.output


def test_cli_clean_error_on_existing_output(
    synthetic_children, synthetic_parents, tmp_path
):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        ["match", str(synthetic_children), str(synthetic_parents), str(output_path)],
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_match_steps(synthetic_children, synthetic_parents, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        match(
            synthetic_children,
            synthetic_parents,
            output_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()


def test_match_all_unassigned(tmp_path):
    """Every child fails to match any parent.

    match() should raise, not silently write an empty output file.
    """
    children_path = tmp_path / "children_far.parquet"
    parents_path = tmp_path / "parents_near.parquet"
    _write_synthetic(children_path, [_CHILD_WKT[3]])  # fid 4, far from any parent
    _write_synthetic(parents_path, _PARENT_WKT)

    output_path = tmp_path / "out.parquet"
    with pytest.raises(RuntimeError, match="no group produced any output"):
        match(children_path, parents_path, output_path, overwrite=True)
