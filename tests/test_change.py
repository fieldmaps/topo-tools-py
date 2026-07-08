"""Portability smoke tests: does change() run to completion on this machine.

Not a topology suite -- the union-find/classification logic in
core/change/_03_classify.py is new and non-obvious enough (ported from
topo-tools-js's classify.ts) that several of these tests assert on specific
classification outcomes, not just "did it run."
"""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.change import change
from topo_tools.cli.main import cli

# Nine spatially-separated regions, each exercising one relationship class (or
# a pair of classes) in isolation. pcode is the sole attribute column, doubling
# as the fixture for column auto-detection (matches _columns.py's "*code$"
# pattern) and as each region's stable identity key across old/new.
#   U1: identical geometry and code, classified unchanged.
#   N1/N1B: identical geometry, code differs; classified unchanged with no
#     linking, renamed under --link-by-code.
#   M1: new geometry shrunk 10% (IoU 0.9, still passes tau_match via
#     coverage_b=1.0); classified modified at default thresholds.
#   SP1/SP2: one old unit splits into two new ones; classified split.
#   MG1/MG2: two old units merge into one new one; classified merge.
#   CR1: new-only, classified created. RM1: old-only, classified removed.
#   RL1: new geometry shifted so only ~10% overlaps (below tau_match);
#     classified removed+created with no linking, relocated under
#     --link-by-code (the pair still touches, just below tau_match).
#   GD1/GD2: a genuine split where only one new unit inherits the old code;
#     exercises the identity-claim guard, must stay split under
#     --link-by-code rather than collapsing into a false 1:1 identity pair.
_OLD_WKT = [
    ("U1", "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
    ("N1", "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))"),
    ("M1", "POLYGON((20 0, 21 0, 21 1, 20 1, 20 0))"),
    ("SP1", "POLYGON((30 0, 33 0, 33 1, 30 1, 30 0))"),
    ("MG1", "POLYGON((40 0, 41 0, 41 1, 40 1, 40 0))"),
    ("MG2", "POLYGON((41 0, 43 0, 43 1, 41 1, 41 0))"),
    ("RM1", "POLYGON((60 0, 61 0, 61 1, 60 1, 60 0))"),
    ("RL1", "POLYGON((70 0, 71 0, 71 1, 70 1, 70 0))"),
    ("GD1", "POLYGON((90 0, 93 0, 93 1, 90 1, 90 0))"),
]

_NEW_WKT = [
    ("U1", "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
    ("N1B", "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))"),
    ("M1", "POLYGON((20 0, 21 0, 21 0.9, 20 0.9, 20 0))"),
    ("SP1", "POLYGON((30 0, 31 0, 31 1, 30 1, 30 0))"),
    ("SP2", "POLYGON((31 0, 33 0, 33 1, 31 1, 31 0))"),
    ("MG1", "POLYGON((40 0, 43 0, 43 1, 40 1, 40 0))"),
    ("CR1", "POLYGON((50 0, 51 0, 51 1, 50 1, 50 0))"),
    ("RL1", "POLYGON((70.9 0, 71.9 0, 71.9 1, 70.9 1, 70.9 0))"),
    ("GD1", "POLYGON((90 0, 91 0, 91 1, 90 1, 90 0))"),
    ("GD2", "POLYGON((91 0, 93 0, 93 1, 91 1, 91 0))"),
]

_STEPS = ["inputs", "overlap", "classify", "outputs"]


def _write_synthetic(path, wkt_rows):
    values = ", ".join(
        f"('{pcode}', ST_GeomFromText('{wkt}'))" for pcode, wkt in wkt_rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(pcode, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")


@pytest.fixture
def old_layer(tmp_path):
    path = tmp_path / "old.parquet"
    _write_synthetic(path, _OLD_WKT)
    return path


@pytest.fixture
def new_layer(tmp_path):
    path = tmp_path / "new.parquet"
    _write_synthetic(path, _NEW_WKT)
    return path


def _read_changelog(path):
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return conn.execute(f"SELECT * FROM '{path}'").fetchall()


def _class_of(rows, code_a=None, code_b=None):
    """relationship_class for the row matching the given code_a/code_b."""
    for row in rows:
        if (code_a is None or row[0] == code_a) and (
            code_b is None or row[2] == code_b
        ):
            return row[4]
    return None


def test_cli_help():
    result = CliRunner().invoke(cli, ["change", "--help"])
    assert result.exit_code == 0
    assert "Compare two polygon layer versions" in result.output
    assert "Examples:" in result.output


def test_change_full_run(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "out.csv"
    overlay_path = tmp_path / "overlay.parquet"
    change(old_layer, new_layer, output_path, overlay_path, overwrite=True)

    assert output_path.exists()
    assert overlay_path.exists()
    rows = _read_changelog(output_path)
    # code/name columns come from --code-column-a/-b, which default to
    # unresolved (None) unless a link flag is set -- geometry-only mode still
    # classifies correctly, but code_a/code_b are NULL throughout this run.
    classes = {row[4] for row in rows}
    assert classes == {"unchanged", "modified", "split", "merge", "created", "removed"}


def test_change_default_output_paths(old_layer, new_layer):
    change(old_layer, new_layer, overwrite=True)

    expected_output = (
        old_layer.parent / f"{old_layer.stem}_{new_layer.stem}_changelog.csv"
    )
    expected_overlay = expected_output.with_stem(
        expected_output.stem + "_overlay"
    ).with_suffix(old_layer.suffix)
    assert expected_output.exists()
    assert expected_overlay.exists()


def test_change_tau_match_threshold(old_layer, new_layer, tmp_path):
    """RL1's ~10% overlap is below the default tau_match; lowering it links the pair.

    code_column_a/-b are passed explicitly (without a --link-by-code flag) so
    the changelog's display columns are populated for row lookup -- this has
    no effect on classification, which stays purely spatial here.
    """
    default_out = tmp_path / "default.csv"
    change(
        old_layer,
        new_layer,
        default_out,
        tmp_path / "default_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    default_rows = _read_changelog(default_out)
    assert _class_of(default_rows, code_a="RL1") == "removed"
    assert _class_of(default_rows, code_b="RL1") == "created"

    loose_out = tmp_path / "loose.csv"
    change(
        old_layer,
        new_layer,
        loose_out,
        tmp_path / "loose_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        tau_match=0.05,
        overwrite=True,
    )
    loose_rows = _read_changelog(loose_out)
    assert _class_of(loose_rows, code_a="RL1", code_b="RL1") == "modified"


def test_change_tau_same_threshold(old_layer, new_layer, tmp_path):
    """M1's IoU of 0.9 is below the default tau_same but above a loosened one."""
    default_out = tmp_path / "default.csv"
    change(
        old_layer,
        new_layer,
        default_out,
        tmp_path / "default_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    assert _class_of(_read_changelog(default_out), code_a="M1") == "modified"

    loose_out = tmp_path / "loose.csv"
    change(
        old_layer,
        new_layer,
        loose_out,
        tmp_path / "loose_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        tau_same=0.5,
        overwrite=True,
    )
    assert _class_of(_read_changelog(loose_out), code_a="M1") == "unchanged"


def test_change_link_by_code(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "linked.csv"
    change(
        old_layer,
        new_layer,
        output_path,
        tmp_path / "linked_overlay.parquet",
        link_by_code=True,
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    rows = _read_changelog(output_path)
    assert _class_of(rows, code_a="N1", code_b="N1B") == "renamed"
    assert _class_of(rows, code_a="RL1", code_b="RL1") == "relocated"


def test_change_identity_claim_guard(old_layer, new_layer, tmp_path):
    """A genuine split (GD1 -> GD1 + GD2) must not collapse into a false 1:1 match."""
    output_path = tmp_path / "guard.csv"
    change(
        old_layer,
        new_layer,
        output_path,
        tmp_path / "guard_overlay.parquet",
        link_by_code=True,
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    rows = _read_changelog(output_path)
    assert _class_of(rows, code_a="GD1", code_b="GD1") == "split"
    assert _class_of(rows, code_a="GD1", code_b="GD2") == "split"


def test_change_column_auto_detection(old_layer, new_layer, tmp_path):
    """--link-by-code without an explicit column still finds pcode via regex."""
    output_path = tmp_path / "auto.csv"
    change(
        old_layer,
        new_layer,
        output_path,
        tmp_path / "auto_overlay.parquet",
        link_by_code=True,
        overwrite=True,
    )
    rows = _read_changelog(output_path)
    assert _class_of(rows, code_a="N1", code_b="N1B") == "renamed"


def test_change_invalid_output_format(old_layer, new_layer, tmp_path):
    with pytest.raises(ValueError, match="tabular"):
        change(old_layer, new_layer, tmp_path / "out.gpkg", overwrite=True)


def test_change_invalid_overlay_format(old_layer, new_layer, tmp_path):
    with pytest.raises(ValueError, match="overlay"):
        change(
            old_layer,
            new_layer,
            tmp_path / "out.csv",
            tmp_path / "overlay.csv",
            overwrite=True,
        )


def test_cli_positional_args(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "cli_out.csv"
    result = CliRunner().invoke(
        cli, ["change", str(old_layer), str(new_layer), str(output_path)]
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_change_error_on_existing_output(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "exists.csv"
    output_path.touch()
    result = CliRunner().invoke(
        cli, ["change", str(old_layer), str(new_layer), str(output_path)]
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_change_steps(old_layer, new_layer, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.csv"
    overlay_path = tmp_path / "steps_overlay.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        change(
            old_layer,
            new_layer,
            output_path,
            overlay_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()
    assert overlay_path.exists()
