"""Public API: extend polygon boundaries outward with Voronoi diagrams."""

import shutil
import signal
import tempfile
from logging import getLogger
from pathlib import Path
from types import FrameType
from typing import Never

from topo_tools.core.duckdb_utils import (
    cleanup_tmp,
    export_debug_tables,
    get_connection,
    log_file,
)
from topo_tools.core.extend import _01_inputs as inputs
from topo_tools.core.extend import _02_lines as lines
from topo_tools.core.extend import _05_merge as merge
from topo_tools.core.extend import _06_outputs as outputs
from topo_tools.core.extend import attempt

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "lines", "attempt", "merge", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "lines": ["{n}_02"],
    "attempt": [
        "{n}_03a",
        "{n}_03_tmp1",
        "{n}_03_tmp2",
        "{n}_03_tmp3",
        "{n}_03_tmp4",
        "{n}_03b",
        "{n}_04",
        "{n}_04_tmp1",
        "{n}_04_tmp2",
    ],
    "merge": ["{n}_05", "{n}_05_tmp1", "{n}_05_tmp2", "{n}_05_tmp3"],
    "outputs": [],
}


def extend(  # noqa: C901, PLR0912, PLR0913, PLR0915
    input_path: str | Path,
    output_path: str | Path,
    *,
    memory_gb: float = 4.0,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = False,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Extend polygon boundaries outward with Voronoi diagrams to fill coverage gaps.

    Processes exactly one file per call. Batch processing many files in one
    process is a CLI-only concern (see topo_tools.cli) — GEOS's native heap
    isn't reliably reclaimed between files, so this function intentionally
    does not loop.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    input_path = Path(input_path)
    output_path = Path(output_path)
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

    name = input_path.name.replace(".", "_")
    if not step:
        cleanup_tmp(name, tmp_dir_path, parquet=True)

    with log_file(name, tmp_dir_path):
        conn = get_connection(name, tmp_dir_path, threads=threads, debug=debug)

        def _interrupt(_sig: int, _frame: FrameType | None) -> Never:
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
                elif s == "lines":
                    lines.main(conn, name)
                elif s == "attempt":
                    attempt.main(conn, name, memory_gb=memory_gb, debug=debug)
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
