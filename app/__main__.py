"""Entry point for edge-extender, discovers inputs and runs the pipeline."""

from logging import getLogger

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
    tmp_dir,
)
from .utils import cleanup_tmp, export_debug_tables, get_connection

logger = getLogger(__name__)


def main() -> None:
    """Run main function."""
    logger.info(
        "--distance=%s --profile=%s --in-memory=%s", distance, profile, in_memory
    )
    files = [input_file] if input_file else sorted(input_dir.iterdir())
    for path in files:
        if not overwrite and (output_dir / path.name).exists():
            continue
        if not path.is_file() or path.suffix not in FORMATS:
            continue
        name = path.name.replace(".", "_")
        tmp_dir.mkdir(exist_ok=True, parents=True)
        cleanup_tmp(name, parquet=True)
        conn = get_connection(name)
        try:
            if profile:
                logger.info("=== inputs ===")
            inputs.main(conn, name, path)
            if profile:
                logger.info("=== lines ===")
            lines.main(conn, name)
            if profile:
                logger.info("=== attempt ===")
            attempt.main(conn, name)
            if profile:
                logger.info("=== merge ===")
            merge.main(conn, name)
            if profile:
                logger.info("=== outputs ===")
            outputs.main(conn, name, path)
            if debug:
                export_debug_tables(conn)
            logger.info("done: %s", name)
        finally:
            conn.close()
            cleanup_tmp(name)


if __name__ == "__main__":
    main()
