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
# independent of that segment's raw length (fixes the collinearity degeneracy
# in docs/voronoi-memory.md). ST_VoronoiDiagram time on tcd_admin2.parquet
# scales worse than linearly with this value (100->1s, 250->3.9s, 500->7.7s,
# 1000->13.9s, 2000->32.1s), so lower is better; 100 was chosen as the
# smallest tested value, with zero measured downside on files that don't hit
# the cap at all (chl/idn/phl_admin3 are byte-for-byte unaffected by this
# constant, confirmed empirically, since none of their real segments exceed
# the cap threshold).
MAX_POINTS_PER_SEGMENT = 100
# Memory model for attempt.py's per-file DISTANCE budget. Two DISTANCE-
# INDEPENDENT costs happen before any final resampling even runs — decomposing
# "{name}_02" into real vertex-to-vertex segments, then ST_LineMerge(
# ST_Union_Agg(...)) remerging the "normal" (non-capped) ones per fid — plus
# one DISTANCE-DEPENDENT cost (the final ST_VoronoiDiagram -> join -> union
# sequence in _04_voronoi.py, which scales with the *final* point count that
# DISTANCE controls). All three measured via standalone probes run inside a
# real --memory=4g --memory-swap=4g (no swap) Docker container — the actual
# deployment constraint, not RSS inference on an unconstrained dev machine.
#
# Segment decompose+remerge cost (DISTANCE-independent, scales with each
# file's own raw vertex count — NOT a global constant, since files vary by
# orders of magnitude here):
#   idn_admin3: 2.49M raw segments -> 1788MB combined peak (~719 B/segment)
#   chl_admin3: 3.23M raw segments -> 2544MB combined peak (~787 B/segment)
#   phl_admin3: 13.07M raw segments -> decompose ALONE already 3505MB, then
#     OOM during remerge (confirmed real kernel SIGKILL via `docker inspect
#     .State.OOMKilled`, not a catchable DuckDBError) — i.e. this cost alone
#     already exceeds 4GB before DISTANCE is ever applied, for a file with
#     enough raw vertices. No DISTANCE value fixes this — doubling DISTANCE
#     in the retry loop never reduces raw vertex count — so attempt.py warns
#     and falls back to the plain default distance instead of computing a
#     budget-derived one that would be nonsensical here; --memory-gb is a
#     soft target, not a hard limit (the real deployment may have swap
#     headroom beyond it), so this never blocks the attempt outright.
_REMERGE_BYTES_PER_RAW_SEGMENT = 850  # rounded up from Chile's 787 B/segment

# Final point cost (DISTANCE-dependent — the ST_VoronoiDiagram -> join ->
# union sequence over the *final* resampled point count):
#   real Chile points:    1.20M->2606MB, 1.49M->3240MB, 1.75M->3604MB, 2.01M->OOM
#   synthetic random pts: 1.00M->2405MB, 1.25M->3263MB, 1.50M->3411MB, 2.00M->OOM
# Fitted model (real-data points): peak_MB ~= 458 + 1.82 * point_count_millions.
_BASELINE_OVERHEAD_MB = 500  # fixed app/DuckDB/spatial-extension startup cost
_BYTES_PER_POINT = 1900  # slightly above the fitted 1820 for headroom
_SAFETY_MARGIN = 0.7  # only plan to use 70% of the theoretical remainder —
# covers pipeline stages outside these probes (inputs/lines/merge), geometry
# shapes worse than the reference files above, and the small sample size
# behind both fitted constants.

# The actual per-file target point budget (used by attempt.py to derive
# effective_distance) can't be computed here — it depends on each file's own
# raw segment count, which isn't known until "{name}_02" exists. See
# attempt.py's main().

_PARQUET_EXPORT = (
    "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2')"
)
COPY_OPTS = {
    ".parquet": _PARQUET_EXPORT,
    ".gpkg": "WITH (FORMAT GDAL, DRIVER 'GPKG')",
    ".geojson": "WITH (FORMAT GDAL, DRIVER 'GeoJSON')",
    ".shp": "WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile')",
}
