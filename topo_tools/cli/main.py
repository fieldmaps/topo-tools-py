"""topo-tools CLI: click entry point."""

from logging import INFO, basicConfig, getLogger
from pathlib import Path

import click

from topo_tools.api import extend as _extend

basicConfig(level=INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger(__name__)


@click.group()
def cli() -> None:
    """topo-tools: DuckDB-powered geospatial topology utilities."""


@cli.command()
@click.option("--input-file", envvar="INPUT_FILE", required=True, help="Input file.")
@click.option("--output-file", envvar="OUTPUT_FILE", required=True, help="Output file.")
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Intermediate DuckDB + Parquet location.",
)
@click.option(
    "--threads", envvar="THREADS", type=int, default=None, help="DuckDB thread count."
)
@click.option(
    "--memory-gb",
    envvar="MEMORY_GB",
    type=float,
    default=4.0,
    show_default=True,
    help="Available memory in GB; sizes point density automatically.",
)
@click.option(
    "--overwrite", envvar="OVERWRITE", is_flag=True, help="Overwrite existing output."
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export to Parquet, log timing/memory per query.",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "lines", "attempt", "merge", "outputs"]),
    default=None,
    help="Run only one named stage.",
)
def extend(  # noqa: PLR0913
    input_file: str,
    output_file: str,
    tmp_dir: str | None,
    threads: int | None,
    memory_gb: float,
    overwrite: bool,  # noqa: FBT001
    debug: bool,  # noqa: FBT001
    step: str | None,
) -> None:
    """Extend polygon boundaries outward with Voronoi diagrams to fill coverage gaps."""
    logger.info("--memory-gb=%s --debug=%s", memory_gb, debug)
    _extend(
        Path(input_file),
        Path(output_file),
        memory_gb=memory_gb,
        threads=threads,
        tmp_dir=tmp_dir,
        overwrite=overwrite,
        debug=debug,
        step=step,
    )
