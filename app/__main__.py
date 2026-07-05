"""Entry point for edge-extender, discovers inputs and runs the pipeline."""

import signal
import sys
from logging import getLogger
from pathlib import Path
from subprocess import run as run_subprocess
from types import FrameType
from typing import Never

from . import _01_inputs as inputs
from . import _02_lines as lines
from . import _05_merge as merge
from . import _06_outputs as outputs
from . import attempt
from .config import (
    FORMATS,
    debug,
    distance,
    input_dir,
    input_file,
    num_threads,
    output_dir,
    overwrite,
    step,
    tmp_dir,
)
from .utils import cleanup_tmp, export_debug_tables, get_connection, log_file

logger = getLogger(__name__)

_STEPS = {
    "inputs": inputs.main,
    "lines": lambda conn, name, _path: lines.main(conn, name),
    "attempt": lambda conn, name, _path: attempt.main(conn, name),
    "merge": lambda conn, name, _path: merge.main(conn, name),
    "outputs": outputs.main,
}

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "lines": ["{n}_02"],
    "attempt": ["{n}_03a", "{n}_03b", "{n}_04", "{n}_04_tmp1", "{n}_04_tmp2"],
    "merge": ["{n}_05", "{n}_05_tmp1", "{n}_05_tmp2", "{n}_05_tmp3"],
    "outputs": [],
}


def main() -> None:
    """Run main function."""
    logger.info("--distance=%s --debug=%s", distance, debug)
    if input_file:
        # Single-file invocation: the recursive base case, run in this process.
        _run_file(input_file)
        return
    for path in sorted(input_dir.iterdir()):
        if not overwrite and (output_dir / path.name).exists():
            continue
        if path.is_file() and path.suffix in FORMATS:
            _run_isolated(path)


def _run_isolated(path: Path) -> None:
    """Run one file in a fresh subprocess so its memory is fully reclaimed on exit.

    GEOS's native heap (used by ST_VoronoiDiagram, ST_Union_Agg, coverage-clean)
    allocates outside DuckDB's own buffer-pool tracking, so RSS is not guaranteed
    to return to the OS between files within one long-lived process even when each
    file's DuckDB connection is properly closed — confirmed empirically to reach
    ~20GB over a batch of files that each peak under 3GB in isolation. Shelling out
    per file sidesteps this regardless of which allocator is actually retaining it.
    """
    args = [
        sys.executable,
        "-m",
        "app",
        f"--input-file={path.name}",
        f"--input-dir={input_dir}",
        f"--output-dir={output_dir}",
        f"--tmp-dir={tmp_dir}",
        f"--distance={distance}",
    ]
    if num_threads is not None:
        args.append(f"--threads={num_threads}")
    if overwrite:
        args.append("--overwrite")
    if debug:
        args.append("--debug")
    if step:
        args.append(f"--step={step}")
    result = run_subprocess(args, check=False)
    if result.returncode != 0:
        logger.error("subprocess failed: %s (exit %s)", path.name, result.returncode)


def _run_file(path: Path) -> None:
    name = path.name.replace(".", "_")
    tmp_dir.mkdir(exist_ok=True, parents=True)
    if not step:
        cleanup_tmp(name, parquet=True)
    with log_file(name):
        conn = get_connection(name)

        def _interrupt(_sig: int, _frame: FrameType | None) -> Never:
            conn.interrupt()
            raise KeyboardInterrupt

        old_handler = signal.signal(signal.SIGINT, _interrupt)
        try:
            logger.info("starting: %s", name)
            for s, fn in _STEPS.items():
                if not step or step == s:
                    if debug:
                        logger.info("=== %s ===", s)
                    fn(conn, name, path)
            if debug:
                only = None
                if step and step in _STEP_TABLES:
                    only = {t.format(n=name) for t in _STEP_TABLES[step]}
                export_debug_tables(conn, only=only)
            logger.info("done: %s", name)
        finally:
            signal.signal(signal.SIGINT, old_handler)
            conn.close()
            if not step and not debug:
                cleanup_tmp(name)


if __name__ == "__main__":
    main()
