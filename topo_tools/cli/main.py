"""topo-tools CLI: click entry point."""

import sys
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from subprocess import run as run_subprocess

import click

from topo_tools.api import extend as _extend
from topo_tools.core.extend._constants import FORMATS

basicConfig(level=INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger(__name__)


@click.group()
def cli() -> None:
    """topo-tools: DuckDB-powered geospatial topology utilities."""


@cli.command()
@click.option(
    "--input-dir",
    envvar="INPUT_DIR",
    default=None,
    help="Input directory (for multiple files).",
)
@click.option(
    "--input-file",
    envvar="INPUT_FILE",
    default=None,
    help="Input file (for a single file).",
)
@click.option(
    "--output-dir",
    envvar="OUTPUT_DIR",
    default=None,
    help="Output directory (for multiple files).",
)
@click.option(
    "--output-file",
    envvar="OUTPUT_FILE",
    default=None,
    help="Output file (for a single file).",
)
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
    input_dir: str | None,
    input_file: str | None,
    output_dir: str | None,
    output_file: str | None,
    tmp_dir: str | None,
    threads: int | None,
    memory_gb: float,
    overwrite: bool,  # noqa: FBT001
    debug: bool,  # noqa: FBT001
    step: str | None,
) -> None:
    """Extend polygon boundaries outward with Voronoi diagrams to fill coverage gaps."""
    logger.info("--memory-gb=%s --debug=%s", memory_gb, debug)

    if input_file:
        input_path = Path(input_file)
        if not input_path.is_absolute() and input_dir:
            input_path = Path(input_dir) / input_path
        output_path = (
            Path(output_file)
            if output_file
            else Path(output_dir or ".") / input_path.name
        )
        _extend(
            input_path,
            output_path,
            memory_gb=memory_gb,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
        return

    if not input_dir:
        msg = "either --input-file or --input-dir is required"
        raise click.UsageError(msg)

    input_dir_path = Path(input_dir)
    output_dir_path = Path(output_dir) if output_dir else input_dir_path
    for path in sorted(input_dir_path.iterdir()):
        if not overwrite and (output_dir_path / path.name).exists():
            continue
        if path.is_file() and path.suffix in FORMATS:
            _run_isolated(
                path,
                input_dir_path,
                output_dir_path,
                tmp_dir,
                memory_gb,
                threads,
                overwrite=overwrite,
                debug=debug,
                step=step,
            )


def _run_isolated(  # noqa: PLR0913
    path: Path,
    input_dir: Path,
    output_dir: Path,
    tmp_dir: str | None,
    memory_gb: float,
    threads: int | None,
    *,
    overwrite: bool,
    debug: bool,
    step: str | None,
) -> None:
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
        "topo_tools",
        "extend",
        f"--input-file={path.name}",
        f"--input-dir={input_dir}",
        f"--output-dir={output_dir}",
        f"--memory-gb={memory_gb}",
    ]
    if tmp_dir:
        args.append(f"--tmp-dir={tmp_dir}")
    if threads is not None:
        args.append(f"--threads={threads}")
    if overwrite:
        args.append("--overwrite")
    if debug:
        args.append("--debug")
    if step:
        args.append(f"--step={step}")
    result = run_subprocess(args, check=False)
    if result.returncode != 0:
        logger.error("subprocess failed: %s (exit %s)", path.name, result.returncode)
