"""Entry point for edge-extender, discovers inputs and runs the pipeline."""

from logging import getLogger

from . import attempt, inputs, lines, merge, outputs
from .config import distance, input_dir, input_file, output_dir, overwrite, tmp_dir
from .utils import cleanup_tmp, get_connection, get_gpkg_layers, is_polygon

logger = getLogger(__name__)


def main() -> None:
    """Run main function."""
    logger.info("--distance=%s", distance)
    files = [input_file] if input_file else sorted(input_dir.iterdir())
    for file in files:
        if not overwrite and (output_dir / file.name).exists():
            continue
        layers = []
        if (
            file.is_file()
            and file.suffix in [".shp", ".geojson", ".parquet"]
            and is_polygon(file)
        ):
            layers = [(file.name.replace(".", "_"), file.stem)]
        elif file.is_file() and file.suffix == ".gpkg":
            layers = [
                (f"{file.name.replace('.', '_')}_{layer}", layer)
                for layer in get_gpkg_layers(file)
            ]
        for name, layer in layers:
            tmp_dir.mkdir(exist_ok=True, parents=True)
            cleanup_tmp(name)
            conn = get_connection(name)
            try:
                inputs.main(conn, name, file, layer)
                lines.main(conn, name)
                attempt.main(conn, name, file, layer)
                merge.main(conn, name)
                outputs.main(conn, name, file, layer)
                logger.info("done: %s", name)
            finally:
                conn.close()
                cleanup_tmp(name)


if __name__ == "__main__":
    main()
