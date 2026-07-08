"""Public API: detect and fix coverage defects (gaps, overlaps) in a polygon layer."""

import shutil
import signal
import tempfile
from logging import getLogger
from pathlib import Path
from types import FrameType
from typing import NoReturn

from topo_tools.core.clean import _01_inputs as inputs
from topo_tools.core.clean import _02_issues as issues
from topo_tools.core.clean import _03_clean as clean_stage
from topo_tools.core.clean import _04_outputs as outputs
from topo_tools.core.clean._constants import SLIVER_TOLERANCE_DEFAULT_M
from topo_tools.core.duckdb_utils import (
    cleanup_tmp,
    export_debug_tables,
    get_connection,
    log_file,
)

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "issues", "clean", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "issues": ["{n}_02"],
    "clean": ["{n}_03"],
    "outputs": [],
}


def _parse_gap_width(value: str) -> tuple[str, float | None]:
    if value in ("auto", "all"):
        return value, None
    try:
        return "value", float(value)
    except ValueError:
        msg = f"--gap-width must be 'auto', 'all', or a number in meters, got {value!r}"
        raise ValueError(msg) from None


def _parse_snap_tolerance(value: str) -> tuple[str, float | None]:
    if value == "auto":
        return "auto", None
    try:
        return "value", float(value)
    except ValueError:
        msg = f"--snap-tolerance must be 'auto' or a number in meters, got {value!r}"
        raise ValueError(msg) from None


def clean(  # noqa: C901, PLR0912, PLR0913, PLR0915
    input_path: str | Path,
    output_path: str | Path | None = None,
    issues_path: str | Path | None = None,
    *,
    gap_width: str = "all",
    snap_tolerance: str = "auto",
    sliver_tolerance_m: float = SLIVER_TOLERANCE_DEFAULT_M,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = False,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Detect and fix gap/overlap defects in a single polygon layer.

    Processes exactly one file per call. Slivers (near-miss boundary
    mismatches) are detected and reported but never auto-fixed -- widening
    ST_CoverageClean's snap tolerance to force one closed re-nodes the whole
    coverage, not just the defect site. Always writes two files: the cleaned
    dataset (output_path, "_cleaned" suffix if omitted) and an issues report
    (issues_path, "_issues" suffix if omitted) so a human can review flagged
    slivers -- and any gaps left unfilled -- before deciding what to do with
    them.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    parsed_gap_width = _parse_gap_width(gap_width)
    parsed_snap_tolerance = _parse_snap_tolerance(snap_tolerance)

    input_path = Path(input_path)
    output_path = (
        Path(output_path)
        if output_path is not None
        else input_path.with_stem(input_path.stem + "_cleaned")
    )
    issues_path = (
        Path(issues_path)
        if issues_path is not None
        else output_path.with_stem(output_path.stem + "_issues")
    )
    if issues_path.suffix == ".shp":
        msg = (
            "issues file cannot be Shapefile: the issues report mixes Polygon "
            "(gap/overlap) and LineString (sliver) geometry in one table, which "
            "Shapefile's single-geometry-type-per-file format can't represent"
        )
        raise ValueError(msg)
    if output_path.exists() and not overwrite:
        msg = f"output already exists: {output_path}"
        raise FileExistsError(msg)
    if issues_path.exists() and not overwrite:
        msg = f"output already exists: {issues_path}"
        raise FileExistsError(msg)

    owns_tmp_dir = tmp_dir is None
    tmp_dir_path = (
        Path(tmp_dir)
        if tmp_dir is not None
        else Path(tempfile.mkdtemp(prefix="topo_tools_"))
    )
    tmp_dir_path.mkdir(exist_ok=True, parents=True)

    name = input_path.name.replace(".", "_") + "_clean"
    if not step:
        cleanup_tmp(name, tmp_dir_path, parquet=True)

    with log_file(name, tmp_dir_path):
        conn = get_connection(name, tmp_dir_path, threads=threads, debug=debug)

        def _interrupt(_sig: int, _frame: FrameType | None) -> NoReturn:
            conn.interrupt()
            raise KeyboardInterrupt

        old_handler = signal.signal(signal.SIGINT, _interrupt)
        try:
            logger.info("starting: %s", name)
            for s in _STEP_ORDER:
                if step and step != s:
                    continue
                if debug:
                    logger.info("=== %s ===", s)
                if s == "inputs":
                    inputs.main(conn, name, input_path)
                elif s == "issues":
                    issues.main(
                        conn, name, sliver_tolerance_m=sliver_tolerance_m, debug=debug
                    )
                elif s == "clean":
                    clean_stage.main(
                        conn,
                        name,
                        gap_width=parsed_gap_width,
                        snap_tolerance=parsed_snap_tolerance,
                        debug=debug,
                    )
                elif s == "outputs":
                    outputs.main(conn, name, output_path, issues_path, debug=debug)
            if debug:
                only = None
                if step and step in _STEP_TABLES:
                    only = {t.format(n=name) for t in _STEP_TABLES[step]}
                export_debug_tables(conn, tmp_dir_path, only=only)
            logger.info("done: %s", name)
        finally:
            signal.signal(signal.SIGINT, old_handler)
            conn.close()
            if not step and not debug:
                cleanup_tmp(name, tmp_dir_path)
            if owns_tmp_dir:
                if debug:
                    logger.info("tmp_dir preserved for --debug: %s", tmp_dir_path)
                else:
                    shutil.rmtree(tmp_dir_path, ignore_errors=True)
