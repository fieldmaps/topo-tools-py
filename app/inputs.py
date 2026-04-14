from pathlib import Path
from subprocess import run

import duckdb

from .utils import _PARQUET_OPTS, coverage_clean, parquet


def main(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    file: Path,
    layer: str,
    *_: list,
) -> None:
    """Import geodata into Parquet with topology cleaning."""
    attr_tmp = parquet(f"{name}_attr_tmp")

    # Import to temp Parquet (all columns including geom)
    run(
        [
            *["gdal", "vector", "convert"],
            *[file, attr_tmp],
            "--overwrite",
            "--quiet",
            f"--input-layer={layer}",
            "--layer-creation-option=FID=fid",
            "--layer-creation-option=GEOMETRY_NAME=geom",
            "--layer-creation-option=COMPRESSION=ZSTD",
            "--layer-creation-option=COMPRESSION_LEVEL=15",
        ],
        check=True,
    )

    # Write _attr: all attributes without geometry
    conn.execute(f"""
        COPY (
            SELECT * EXCLUDE (geom)
            FROM read_parquet('{attr_tmp}')
        ) TO '{parquet(f"{name}_attr")}' {_PARQUET_OPTS}
    """)

    # Write _01_pre: force 2D, reproject to EPSG:4326, validate, force multi
    pre = parquet(f"{name}_01_pre")
    conn.execute(f"""
        COPY (
            SELECT
                fid,
                ST_Multi(ST_MakeValid(
                    ST_Transform(ST_Force2D(geom), 'EPSG:4326')
                )) AS geom
            FROM read_parquet('{attr_tmp}')
        ) TO '{pre}' {_PARQUET_OPTS}
    """)
    Path(attr_tmp).unlink()

    # Coverage clean: snap shared boundaries, fill slivers
    coverage_clean(pre, parquet(f"{name}_01"))
    Path(pre).unlink()
