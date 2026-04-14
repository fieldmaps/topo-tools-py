from argparse import ArgumentParser
from decimal import Decimal
from logging import INFO, basicConfig
from multiprocessing import cpu_count
from os import environ, getenv
from pathlib import Path

from dotenv import load_dotenv


def is_bool(string: str) -> bool:
    """Check if string is boolean-like."""
    return string.upper() in ("YES", "ON", "TRUE", "1")


load_dotenv(override=True)
basicConfig(level=INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

environ["OGR_GEOJSON_MAX_OBJ_SIZE"] = "0"
cwd = Path(__file__).parent

DBNAME = getenv("DBNAME", "app")
DISTANCE = getenv("DISTANCE", "0.0002")
INPUT_DIR = getenv("INPUT_DIR", str(cwd / "../inputs"))
OUTPUT_DIR = getenv("OUTPUT_DIR", str(cwd / "../outputs"))
OVERWRITE = getenv("OVERWRITE", "NO")
NUM_THREADS = getenv("NUM_THREADS", str(cpu_count()))
QUIET = getenv("QUIET", "NO")

parser = ArgumentParser(description="Extend geometry edges.")
parser.add_argument("--dbname", help="run in a different local database (default: app)")
parser.add_argument("--input-dir", help="input directory (for multiple files)")
parser.add_argument("--input-file", help="input file (for single files)")
parser.add_argument("--output-dir", help="output directory (for multiple files)")
parser.add_argument("--output-file", help="output file (for single files)")
parser.add_argument(
    "--distance",
    help="decimal degrees between points on a line (default: 0.0002)",
)
parser.add_argument(
    "--num-threads",
    help="number of layers to run at once. (default: 1 * number of CPUs detected)",
)
parser.add_argument(
    "--overwrite",
    help="whether to overwrite existing files (default: no)",
)
parser.add_argument("--quiet", help="Suppress info and error messages (default: no)")

args = parser.parse_args()

dbname = args.dbname or DBNAME
distance = Decimal(args.distance or DISTANCE)
input_dir = Path(args.input_dir or INPUT_DIR)
input_file = Path(args.input_file) if args.input_file else None
num_threads = int(args.num_threads or NUM_THREADS)
output_dir = Path(args.output_dir or OUTPUT_DIR)
output_file = Path(args.output_file) if args.output_file else None
overwrite = is_bool(args.overwrite or OVERWRITE)
quiet = is_bool(args.quiet or QUIET)
