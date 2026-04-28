"""Entry point for edge-extender, discovers inputs and runs the pipeline."""

from logging import getLogger
from pathlib import Path

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
from .utils import ProfiledConnection, cleanup_tmp, export_debug_tables, get_connection

logger = getLogger(__name__)

_STAGES = ["inputs", "lines", "attempt", "merge", "outputs"]


def _run_stage(conn: ProfiledConnection, name: str, path: Path, s: str) -> None:
    if profile:
        logger.info("=== %s ===", s)
    if s == "inputs":
        inputs.main(conn, name, path)
    elif s == "lines":
        lines.main(conn, name)
    elif s == "attempt":
        attempt.main(conn, name)
    elif s == "merge":
        merge.main(conn, name)
    elif s == "outputs":
        outputs.main(conn, name, path)


def _run_file(path: Path) -> None:
    name = path.name.replace(".", "_")
    tmp_dir.mkdir(exist_ok=True, parents=True)
    if not stage:
        cleanup_tmp(name, parquet=True)
    conn = get_connection(name)
    try:
        for s in _STAGES:
            if not stage or stage == s:
                _run_stage(conn, name, path, s)
        if debug:
            export_debug_tables(conn)
        logger.info("done: %s", name)
    finally:
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
