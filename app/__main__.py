"""Entry point for edge-extender, discovers inputs and runs the pipeline."""

from logging import getLogger

from . import attempt, inputs, lines, merge, outputs
from .config import (
    FORMATS,
    debug,
    distance,
    input_dir,
    input_file,
    output_dir,
    overwrite,
    tmp_dir,
)
from .utils import cleanup_tmp, export_debug_tables, get_connection

logger = getLogger(__name__)


def main() -> None:
    """Run main function."""
    logger.info("--distance=%s", distance)
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
            inputs.main(conn, name, path)
            lines.main(conn, name)
            attempt.main(conn, name)
            merge.main(conn, name)
            outputs.main(conn, name, path)
            if debug:
                export_debug_tables(conn)
            logger.info("done: %s", name)
        finally:
            conn.close()
            cleanup_tmp(name)


if __name__ == "__main__":
    main()
