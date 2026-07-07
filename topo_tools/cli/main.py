"""topo-tools CLI: click entry point."""

from logging import INFO, basicConfig, getLogger
from pathlib import Path

import click

from topo_tools.api import extend as _extend

basicConfig(level=INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger(__name__)


@click.group()
@click.version_option(package_name="topo-tools", prog_name="topo-tools")
def cli() -> None:
    """topo-tools: DuckDB-powered geospatial topology utilities."""


@cli.command()
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
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
    "--threads", envvar="THREADS", type=int, default=None, help="DuckDB thread count."
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export to Parquet, log timing/memory per query.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Intermediate DuckDB + Parquet location.",
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
    output_file: str | None,
    memory_gb: float,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    r"""Extend polygon boundaries outward with Voronoi diagrams to fill coverage gaps.

    OUTPUT_FILE defaults to INPUT_FILE with an "_extended" suffix if omitted.

    \b
    Examples:
      # Basic run, output name chosen automatically
      topo-tools extend example.geojson

      \b
      # Explicit output, sized for a 2GB container
      topo-tools extend example.gpkg example_extended.gpkg --memory-gb 2

      \b
      # Rerun and overwrite a previous output
      topo-tools extend example.parquet example_extended.parquet --overwrite
    """
    logger.info("--memory-gb=%s --debug=%s", memory_gb, debug)
    try:
        _extend(
            Path(input_file),
            Path(output_file) if output_file is not None else None,
            memory_gb=memory_gb,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e
