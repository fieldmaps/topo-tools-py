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
parser.add_argument("--gap-max-width", default=getenv("GAP_MAX_WIDTH", "0.0001"))
parser.add_argument("--gap-max-thinness", default=getenv("GAP_MAX_THINNESS", "0.05"))
parser.add_argument(
    "--overlap-strategy",
    default=getenv("OVERLAP_STRATEGY", "merge_longest_border"),
    choices=["largest_area", "merge_longest_border"],
)
parser.add_argument("--threads", default=getenv("THREADS"))
parser.add_argument("--overwrite", **_bool_flag("OVERWRITE"))
parser.add_argument("--debug", **_bool_flag("DEBUG"))
parser.add_argument("--profile", **_bool_flag("PROFILE"))
parser.add_argument("--in-memory", **_bool_flag("IN_MEMORY"))
parser.add_argument(
    "--stage",
    default=getenv("STAGE"),
    choices=["inputs", "clean", "lines", "attempt", "merge", "outputs"],
)
args = parser.parse_args()

distance = Decimal(args.distance)
gap_max_width = float(args.gap_max_width)
gap_max_thinness = float(args.gap_max_thinness)
overlap_strategy = args.overlap_strategy
num_threads = int(args.threads) if args.threads is not None else None
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
debug = args.debug or bool(args.stage)
profile = args.profile
in_memory = args.in_memory
stage = args.stage

FORMATS = [".shp", ".geojson", ".parquet", ".gpkg"]

MAX_POINTS = 10_000_000
SNAP_TOLERANCE = 0.00000001

_PARQUET_EXPORT = (
    "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2')"
)
COPY_OPTS = {
    ".parquet": _PARQUET_EXPORT,
    ".gpkg": "WITH (FORMAT GDAL, DRIVER 'GPKG')",
    ".geojson": "WITH (FORMAT GDAL, DRIVER 'GeoJSON')",
    ".shp": "WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile')",
}
