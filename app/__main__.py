import logging

from . import attempt, inputs, lines, merge, outputs
from .config import (
    distance,
    input_dir,
    input_file,
    output_dir,
    overwrite,
)
from .utils import apply_funcs, get_gpkg_layers, is_polygon

logger = logging.getLogger(__name__)

funcs = [inputs.main, lines.main, attempt.main, merge.main, outputs.main]


def main() -> None:
    """Run main function."""
    logger.info("--distance=%s", distance)
    files = [input_file] if input_file else sorted(input_dir.iterdir())
    for file in files:
        if not overwrite and (output_dir / file.name).exists():
            continue
        if (
            file.is_file()
            and file.suffix in [".shp", ".geojson", ".parquet"]
            and is_polygon(file)
        ):
            apply_funcs(file.name.replace(".", "_"), file, file.stem, *funcs)
        if file.is_file() and file.suffix == ".gpkg":
            for layer in get_gpkg_layers(file):
                apply_funcs(
                    f"{file.name.replace('.', '_')}_{layer}", file, layer, *funcs
                )


if __name__ == "__main__":
    main()
