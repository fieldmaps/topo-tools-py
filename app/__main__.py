"""Entry point for edge-extender, discovers inputs and runs the pipeline."""

import signal
from logging import getLogger
from pathlib import Path
from types import FrameType
from typing import Never

from . import attempt, inputs, lines, merge, outputs
from .config import (
    FORMATS,
    debug,
    distance,
    in_memory,
    input_dir,
    input_file,
    output_dir,
    overwrite,
    profile,
    stage,
    tmp_dir,
)
from .utils import cleanup_tmp, export_debug_tables, get_connection

logger = getLogger(__name__)

_STAGES = {
    "inputs": inputs.main,
    "lines": lambda conn, name, _path: lines.main(conn, name),
    "attempt": lambda conn, name, _path: attempt.main(conn, name),
    "merge": lambda conn, name, _path: merge.main(conn, name),
    "outputs": outputs.main,
}


def _run_file(path: Path) -> None:
    name = path.name.replace(".", "_")
    tmp_dir.mkdir(exist_ok=True, parents=True)
    if not stage:
        cleanup_tmp(name, parquet=True)
    conn = get_connection(name)

    def _interrupt(_sig: int, _frame: FrameType | None) -> Never:
        conn.interrupt()
        raise KeyboardInterrupt

    old_handler = signal.signal(signal.SIGINT, _interrupt)
    try:
        for s, fn in _STAGES.items():
            if not stage or stage == s:
                if profile:
                    logger.info("=== %s ===", s)
                fn(conn, name, path)
        if debug:
            export_debug_tables(conn)
        logger.info("done: %s", name)
    finally:
        signal.signal(signal.SIGINT, old_handler)
        conn.close()
        if not stage and not debug:
            cleanup_tmp(name)


def main() -> None:
    """Run main function."""
    logger.info(
        "--distance=%s --profile=%s --in-memory=%s", distance, profile, in_memory
    )
    files = [input_file] if input_file else sorted(input_dir.iterdir())
    for path in files:
        if not overwrite and (output_dir / path.name).exists():
            continue
        if path.is_file() and path.suffix in FORMATS:
            _run_file(path)


if __name__ == "__main__":
    main()
