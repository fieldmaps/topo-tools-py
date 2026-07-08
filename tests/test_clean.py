"""Portability smoke tests: does clean() run to completion on this machine.

Not a topology/correctness suite for the general pipeline, but the gap/
overlap/sliver *classification* logic is new and non-obvious enough (see
core/clean/_02_issues.py) that a few of these tests do assert on specific
detected/fixed outcomes, not just "did it run."
"""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.clean import clean
from topo_tools.cli.main import cli

# Three independent groups, spatially separated so each exercises exactly one
# defect kind without interference:
#   - fid 1-4: a "donut" of four polygons noded at their shared corners,
#     enclosing a real 1x1 degree gap at (1,1)-(2,2). Enclosure matters --
#     an open inlet between two non-surrounding polygons is NOT detected as
#     a gap by the interior-ring method (GEOS: "gaps not fully enclosed are
#     not removed"), it shows up as a sliver instead.
#   - fid 5-6: fid 6 overlaps fid 5 by 0.05 degrees.
#   - fid 7-8: fid 8 sits 0.00003 degrees (~3.3m) from fid 7 -- within the
#     default 10m sliver tolerance, but not an enclosed gap (open inlet), so
#     it's detected only as a sliver, never auto-fixed.
#   - fid 9-10: fid 10 sits fully inside fid 9 (a duplicated/nested-digitizing
#     defect). The overlap join's predicate is ST_Overlaps OR ST_Contains,
#     not ST_Intersects -- ST_Overlaps alone is false here by OGC definition
#     (the intersection equals fid 10 exactly, not "different from both
#     inputs"), so this pair only gets caught via the ST_Contains half.
_SYNTHETIC_WKT = [
    (1, "POLYGON((0 0, 3 0, 3 1, 2 1, 1 1, 0 1, 0 0))"),
    (2, "POLYGON((0 2, 1 2, 2 2, 3 2, 3 3, 0 3, 0 2))"),
    (3, "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))"),
    (4, "POLYGON((2 1, 3 1, 3 2, 2 2, 2 1))"),
    (5, "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))"),
    (6, "POLYGON((10.95 0, 12 0, 12 1, 10.95 1, 10.95 0))"),
    (7, "POLYGON((20 0, 21 0, 21 1, 20 1, 20 0))"),
    (8, "POLYGON((21.00003 0, 22 0, 22 1, 21.00003 1, 21.00003 0))"),
    (9, "POLYGON((30 0, 32 0, 32 2, 30 2, 30 0))"),
    (10, "POLYGON((30.5 0.5, 31.5 0.5, 31.5 1.5, 30.5 1.5, 30.5 0.5))"),
]

_STEPS = ["inputs", "issues", "clean", "outputs"]


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


def _real_hole_area(path):
    """Area of any fully-enclosed hole in the union of an output file's polygons."""
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return conn.execute(f"""
            WITH u AS (SELECT ST_Union_Agg(geometry) AS g FROM '{path}'),
            parts AS (SELECT (UNNEST(ST_Dump(g))).geom AS geom FROM u)
            SELECT COALESCE(
                SUM(
                    ST_Area(ST_Difference(ST_MakePolygon(ST_ExteriorRing(geom)), geom))
                ),
                0
            )
            FROM parts WHERE ST_NumInteriorRings(geom) > 0
        """).fetchone()[0]


def test_cli_help():
    result = CliRunner().invoke(cli, ["clean", "--help"])
    assert result.exit_code == 0
    assert "Detect and fix gap/overlap defects" in result.output
    assert "Examples:" in result.output


def test_clean_full_run(synthetic_input, tmp_path):
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    clean(synthetic_input, output_path, issues_path, overwrite=True)

    assert output_path.exists()
    assert issues_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        row_count = conn.execute(f"SELECT COUNT(*) FROM '{output_path}'").fetchone()[0]
        kinds = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT kind FROM '{issues_path}'"
            ).fetchall()
        }
    assert row_count == len(_SYNTHETIC_WKT)
    assert kinds == {"gap", "overlap", "sliver"}


def test_clean_detects_full_containment_overlap(synthetic_input, tmp_path):
    """A fully-nested duplicate polygon (id 10 inside id 9) is an overlap.

    Regression for the overlap join predicate: ST_Overlaps alone is false
    for full containment (OGC: the intersection must differ from both
    inputs), so this only gets caught via the ST_Contains half. Located by
    geometry, not unit_a/unit_b -- those reference the internal `fid`
    (row_number() over an unordered scan, since preserve_insertion_order is
    off), which isn't guaranteed to match the source "id" column.
    """
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    clean(synthetic_input, output_path, issues_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        area = conn.execute(f"""
            SELECT ST_Area(geometry) FROM '{issues_path}'
            WHERE kind = 'overlap'
              AND ST_Within(geometry, ST_MakeEnvelope(30, 0, 32, 2))
        """).fetchall()
    # Full containment -- the overlap area equals fid 10's entire 1x1 extent.
    assert area == [(1.0,)]


def test_clean_default_output_paths(synthetic_input):
    clean(synthetic_input, overwrite=True)

    expected_output = synthetic_input.with_stem(synthetic_input.stem + "_cleaned")
    expected_issues = expected_output.with_stem(expected_output.stem + "_issues")
    assert expected_output.exists()
    assert expected_issues.exists()


def test_clean_gap_width_all_fills_gap(synthetic_input, tmp_path):
    output_path = tmp_path / "all.parquet"
    issues_path = tmp_path / "all_issues.parquet"
    clean(synthetic_input, output_path, issues_path, gap_width="all", overwrite=True)

    assert _real_hole_area(output_path) == pytest.approx(0.0, abs=1e-9)


def test_clean_gap_width_auto_leaves_gap(synthetic_input, tmp_path):
    output_path = tmp_path / "auto.parquet"
    issues_path = tmp_path / "auto_issues.parquet"
    clean(synthetic_input, output_path, issues_path, gap_width="auto", overwrite=True)

    assert _real_hole_area(output_path) == pytest.approx(1.0, rel=1e-6)


def test_clean_gap_width_explicit_meters(synthetic_input, tmp_path):
    narrow_output = tmp_path / "narrow.parquet"
    narrow_issues = tmp_path / "narrow_issues.parquet"
    clean(
        synthetic_input, narrow_output, narrow_issues, gap_width="50000", overwrite=True
    )
    assert _real_hole_area(narrow_output) == pytest.approx(1.0, rel=1e-6)

    wide_output = tmp_path / "wide.parquet"
    wide_issues = tmp_path / "wide_issues.parquet"
    clean(synthetic_input, wide_output, wide_issues, gap_width="200000", overwrite=True)
    assert _real_hole_area(wide_output) == pytest.approx(0.0, abs=1e-9)


def test_clean_sliver_never_autofixed(synthetic_input, tmp_path):
    output_path = tmp_path / "sliver.parquet"
    issues_path = tmp_path / "sliver_issues.parquet"
    clean(
        synthetic_input,
        output_path,
        issues_path,
        gap_width="all",
        snap_tolerance="auto",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        sliver_count = conn.execute(
            f"SELECT COUNT(*) FROM '{issues_path}' WHERE kind = 'sliver'"
        ).fetchone()[0]
        # "id" is the source attribute column (fid 7/8's original identity) --
        # the internal "fid" column is dropped on export, matching extend/match.
        # ST_Distance(GEOMETRY, GEOMETRY) is unreliable for two disjoint
        # polygons at this separation (confirmed: returns exactly 0.0 for
        # polygons ~3.3cm apart, vs. the correct ~3e-5 for the equivalent
        # POINT/LINESTRING pair) -- compare X extents directly instead.
        xmax7, xmin8 = conn.execute(f"""
            SELECT
                (SELECT ST_XMax(geometry) FROM '{output_path}' WHERE id = 7),
                (SELECT ST_XMin(geometry) FROM '{output_path}' WHERE id = 8)
        """).fetchone()
    assert sliver_count > 0
    # The near-miss between fid 7/8 is still there -- coverage_clean never
    # widens snapping to close a sliver, so they remain disjoint.
    assert xmax7 < xmin8


def test_clean_invalid_gap_width_value(synthetic_input, tmp_path):
    with pytest.raises(ValueError, match="gap-width"):
        clean(
            synthetic_input,
            tmp_path / "bad.parquet",
            gap_width="potato",
            overwrite=True,
        )


def test_clean_issues_rejects_shapefile(synthetic_input, tmp_path):
    with pytest.raises(ValueError, match="Shapefile"):
        clean(
            synthetic_input,
            tmp_path / "out.gpkg",
            tmp_path / "issues.shp",
            overwrite=True,
        )


def test_cli_positional_args(synthetic_input, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(cli, ["clean", str(synthetic_input), str(output_path)])
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_clean_error_on_existing_output(synthetic_input, tmp_path):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(cli, ["clean", str(synthetic_input), str(output_path)])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_clean_steps(synthetic_input, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.parquet"
    issues_path = tmp_path / "steps_issues.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        clean(
            synthetic_input,
            output_path,
            issues_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()
    assert issues_path.exists()
