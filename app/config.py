from argparse import ArgumentParser
from decimal import Decimal
from logging import INFO, basicConfig
from os import environ, getenv
from pathlib import Path


def is_bool(string: str) -> bool:
    """Convert string to boolean."""
    return string.upper() in ("YES", "ON", "TRUE", "1")


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
parser.add_argument("--overwrite", default=getenv("OVERWRITE", "NO"))

args = parser.parse_args()

distance = Decimal(args.distance)
input_dir = Path(args.input_dir)
input_file = Path(args.input_file) if args.input_file else None
output_dir = Path(args.output_dir)
output_file = Path(args.output_file) if args.output_file else None
tmp_dir = Path(args.tmp_dir)
overwrite = is_bool(args.overwrite)

_parquet_opts = [
    "FORMAT PARQUET",
    "COMPRESSION ZSTD",
    "COMPRESSION_LEVEL 15",
    "GEOPARQUET_VERSION 'NONE'",
]
PARQUET_OPTS = f"({', '.join(_parquet_opts)})"

GDAL_PARQUET_LCO = [
    "--overwrite",
    "--quiet",
    "--layer-creation-option=GEOMETRY_NAME=geometry",
    "--layer-creation-option=USE_PARQUET_GEO_TYPES=ONLY",
    "--layer-creation-option=COMPRESSION=ZSTD",
    "--layer-creation-option=COMPRESSION_LEVEL=15",
]
GDAL_SHP_LCO = ["--layer-creation-option=ENCODING=UTF-8"]
