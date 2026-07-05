"""Parses CLI arguments and environment variables for pipeline configuration."""

from argparse import ArgumentParser
from decimal import Decimal
from logging import INFO, basicConfig
from os import environ, getenv
from pathlib import Path

_BOOL_VALS = ("YES", "ON", "TRUE", "1")


def _bool_flag(env_var: str) -> dict:
    return {
        "default": getenv(env_var) in _BOOL_VALS,
        "type": lambda v: v is None or v.upper() in _BOOL_VALS,
        "nargs": "?",
        "const": True,
    }


basicConfig(level=INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

environ["OGR_GEOJSON_MAX_OBJ_SIZE"] = "0"
cwd = Path(__file__).parent

parser = ArgumentParser(description="Extend geometry edges.")
parser.add_argument("--input-dir", default=getenv("INPUT_DIR", str(cwd / "../inputs")))
parser.add_argument("--input-file", default=getenv("INPUT_FILE"))
parser.add_argument(
    "--output-dir", default=getenv("OUTPUT_DIR", str(cwd / "../outputs"))
)
parser.add_argument("--output-file", default=getenv("OUTPUT_FILE"))
parser.add_argument("--tmp-dir", default=getenv("TMP_DIR", str(cwd / "../tmp")))
parser.add_argument("--distance", default=getenv("DISTANCE", "0.0002"))
parser.add_argument("--threads", default=getenv("THREADS"))
parser.add_argument("--memory-gb", default=getenv("MEMORY_GB", "4"))
parser.add_argument("--overwrite", **_bool_flag("OVERWRITE"))
parser.add_argument("--debug", **_bool_flag("DEBUG"))
parser.add_argument(
    "--step",
    default=getenv("STEP"),
    choices=["inputs", "lines", "attempt", "merge", "outputs"],
)
args = parser.parse_args()

distance = Decimal(args.distance)
num_threads = int(args.threads) if args.threads is not None else None
memory_gb = float(args.memory_gb)
input_dir = Path(args.input_dir)
_input_file = Path(args.input_file) if args.input_file else None
input_file = (
    (_input_file if _input_file.is_absolute() else input_dir / _input_file)
    if _input_file
    else None
)
output_dir = Path(args.output_dir)
output_file = Path(args.output_file) if args.output_file else None
tmp_dir = Path(args.tmp_dir)
overwrite = args.overwrite
debug = args.debug or bool(args.step)
step = args.step

FORMATS = [".shp", ".geojson", ".parquet", ".gpkg"]

MAX_POINTS = 10_000_000
SNAP_TOLERANCE = 0.00000001
# Cap on points generated per real (untouched) line segment; bounds the size
# of the largest exactly-collinear point cluster fed to ST_VoronoiDiagram,
# independent of that segment's raw length. 100 was the smallest of several
# tested values, with zero downside on files that don't hit the cap. See
# docs/voronoi-memory.md for the full timing comparison and rationale.
MAX_POINTS_PER_SEGMENT = 100

# Memory model for attempt.py's per-file DISTANCE budget: a DISTANCE-
# independent segment decompose+remerge floor, a fixed startup overhead, and
# a DISTANCE-dependent final-point cost. All fitted from real
# --memory=4g --memory-swap=4g container probes — see docs/voronoi-memory.md
# for the full derivation and measured data points.
REMERGE_BYTES_PER_RAW_SEGMENT = 850  # ~787 B/segment fitted on chl_admin3, rounded up
BASELINE_OVERHEAD_MB = 500  # fixed app/DuckDB/spatial-extension startup cost
BYTES_PER_POINT = 1900  # ~1820 B/point fitted, rounded up for headroom
SAFETY_MARGIN = 0.7  # only plan to use 70% of the theoretical remainder

_PARQUET_EXPORT = (
    "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2')"
)
COPY_OPTS = {
    ".parquet": _PARQUET_EXPORT,
    ".gpkg": "WITH (FORMAT GDAL, DRIVER 'GPKG')",
    ".geojson": "WITH (FORMAT GDAL, DRIVER 'GeoJSON')",
    ".shp": "WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile')",
}
