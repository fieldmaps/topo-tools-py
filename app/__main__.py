"""Entry point for edge-extender, discovers inputs and runs the pipeline."""

import signal
from logging import getLogger
from pathlib import Path
from types import FrameType
from typing import Never

from . import attempt, clean, inputs, lines, merge, outputs
from .config import (
    FORMATS,
    debug,
    distance,
    input_dir,
    input_file,
    output_dir,
    overwrite,
    step,
    tmp_dir,
)
from .utils import cleanup_tmp, export_debug_tables, get_connection

logger = getLogger(__name__)

_STEPS = {
    "inputs": inputs.main,
    "clean": lambda conn, name, _path: clean.main(conn, name),
    "lines": lambda conn, name, _path: lines.main(conn, name),
    "attempt": lambda conn, name, _path: attempt.main(conn, name),
    "merge": lambda conn, name, _path: merge.main(conn, name),
    "outputs": outputs.main,
}

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "clean": ["{n}_01"],
    "lines": ["{n}_02a", "{n}_02b"],
    "attempt": ["{n}_03a", "{n}_03b", "{n}_04", "{n}_04_tmp1", "{n}_04_tmp2"],
    "merge": ["{n}_05", "{n}_05_tmp1", "{n}_05_tmp2", "{n}_05_tmp3", "{n}_05_tmp4"],
    "outputs": [],
}


def _run_file(path: Path) -> None:
    name = path.name.replace(".", "_")
    tmp_dir.mkdir(exist_ok=True, parents=True)
    if not step:
        cleanup_tmp(name, parquet=True)
    conn = get_connection(name)

    def _interrupt(_sig: int, _frame: FrameType | None) -> Never:
        conn.interrupt()
        raise KeyboardInterrupt

    old_handler = signal.signal(signal.SIGINT, _interrupt)
    try:
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


def main() -> None:
    """Run main function."""
    logger.info("--distance=%s --debug=%s", distance, debug)
    files = [input_file] if input_file else sorted(input_dir.iterdir())
    for path in files:
        if not overwrite and (output_dir / path.name).exists():
            continue
        if path.is_file() and path.suffix in FORMATS:
            _run_file(path)


if __name__ == "__main__":
    main()
