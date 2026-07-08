"""Public API: match child polygons to parent boundaries, then extend to fill gaps."""

import shutil
import signal
import tempfile
from logging import getLogger
from pathlib import Path
from types import FrameType
from typing import NoReturn

from topo_tools.core.duckdb_utils import (
    cleanup_tmp,
    export_debug_tables,
    get_connection,
    log_file,
)
from topo_tools.core.match import _01_inputs as inputs
from topo_tools.core.match import _02_assign as assign
from topo_tools.core.match import _03_groups as groups
from topo_tools.core.match import _04_merge as merge
from topo_tools.core.match import _05_outputs as outputs

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "assign", "groups", "merge", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_child_01", "{n}_parent_01"],
    "assign": ["{n}_02_pairs", "{n}_02_assign", "{n}_02_unassigned"],
    # "groups" is deliberately absent: group ids aren't known ahead of time
    # (dynamic "{n}_g{parent_fid}" names), so it falls through to the
    # "export everything currently in the connection" default below, same as
    # a full (no --step) run.
    "merge": ["{n}_04"],
    "outputs": [],
}


def match(  # noqa: C901, PLR0912, PLR0913, PLR0915
    input_path: str | Path,
    clip_path: str | Path,
    output_path: str | Path | None = None,
    *,
    memory_gb: float = 4.0,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = False,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Match child polygons to their best-overlapping parent, then extend to fill gaps.

    Processes exactly one child file + one parent/clip file per call. Children
    are assigned to whichever parent polygon they share the largest area with,
    grouped by that assignment, extended within each group independently (in
    an isolated subprocess per group), clipped to that group's own parent,
    reassembled, and coverage-cleaned once as a whole. If output_path is
    omitted, it defaults to input_path with a "_matched" suffix in the same
    directory.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    input_path = Path(input_path)
    clip_path = Path(clip_path)
    output_path = (
        Path(output_path)
        if output_path is not None
        else input_path.with_stem(input_path.stem + "_matched")
    )
    if output_path.exists() and not overwrite:
        msg = f"output already exists: {output_path}"
        raise FileExistsError(msg)

    owns_tmp_dir = tmp_dir is None
    tmp_dir_path = (
        Path(tmp_dir)
        if tmp_dir is not None
        else Path(tempfile.mkdtemp(prefix="topo_tools_"))
    )
    tmp_dir_path.mkdir(exist_ok=True, parents=True)

    # "_match" keeps every table/file this call creates distinct from an
    # extend() run against the same input_path/tmp_dir -- e.g. extend's bare
    # "{name}_04" (Voronoi cells) would otherwise collide with match's own
    # bare "{name}_04" (final coverage-cleaned output) if both tools shared a
    # tmp_dir and were run with --debug for side-by-side inspection.
    name = input_path.name.replace(".", "_") + "_match"
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
                    inputs.main(conn, name, input_path, clip_path)
                elif s == "assign":
                    assign.main(conn, name)
                elif s == "groups":
                    groups.main(
                        conn,
                        name,
                        tmp_dir_path,
                        memory_gb=memory_gb,
                        threads=threads,
                        debug=debug,
                    )
                elif s == "merge":
                    merge.main(conn, name, debug=debug)
                elif s == "outputs":
                    outputs.main(conn, name, output_path, debug=debug)
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
