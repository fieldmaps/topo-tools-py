from pathlib import Path
from subprocess import run

import duckdb

from .config import GDAL_PARQUET_LCO, PARQUET_OPTS
from .utils import parquet


def main(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    file: Path,
    layer: str,
    *_: list,
) -> None:
    """Import geodata into Parquet with topology cleaning."""
    attr_tmp = parquet(f"{name}_attr_tmp")

    # Import all columns (attributes only, no geometry processing needed here)
    # fmt: off
    run(
        [
            "gdal", "vector", "convert",
            str(file), f"--input-layer={layer}",
            attr_tmp, "--layer-creation-option=FID=fid", *GDAL_PARQUET_LCO,
        ],
        check=True,
    )
    # fmt: on

    # Write _attr: all attributes without geometry
    conn.execute(f"""--sql
        COPY (
            SELECT * EXCLUDE (geometry)
            FROM read_parquet('{attr_tmp}')
        ) TO '{parquet(f"{name}_attr")}' {PARQUET_OPTS}
    """)

    # Pipeline: fix geometry, force 2D + multi, clean topology
    # fmt: off
    run(
        [
            "gdal", "vector", "pipeline",
            "read", attr_tmp,
            "!", "make-valid",
            "!", "reproject", "--dst-crs=EPSG:4326",
            "!", "set-geom-type", "--multi", "--dim=XY",
            "!", "clean-coverage",
            "!", "write", "--layer-creation-option=FID=fid",
            *GDAL_PARQUET_LCO, parquet(f"{name}_01"),
        ],
        check=True,
    )
    # fmt: on
    Path(attr_tmp).unlink()
