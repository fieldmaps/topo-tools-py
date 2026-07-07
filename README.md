# topo-tools

[![CI](https://github.com/fieldmaps/topo-tools-py/actions/workflows/ci.yml/badge.svg)](https://github.com/fieldmaps/topo-tools-py/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/topo-tools)](https://pypi.org/project/topo-tools/)
[![Python versions](https://img.shields.io/pypi/pyversions/topo-tools)](https://pypi.org/project/topo-tools/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![World ADM0 boundaries extended with Voronoi-filled coastline](https://raw.githubusercontent.com/fieldmaps/topo-tools-py/main/img/wld_01.png)

`topo-tools` is a collection of DuckDB-powered geospatial topology utilities. It
currently ships one tool, **extend**: given a layer of polygons with gaps between
them (missing coastline, disputed areas, water bodies), it extends each polygon
outward to fill the surrounding gaps, producing a complete coverage layer with no
overlaps or holes. Existing polygon boundaries are left untouched except where they
border a gap. See [`docs/examples.md`](docs/examples.md) for how it works and
example use cases.

## Requirements

- Python 3.10+.
- Network access on first run: DuckDB downloads the `spatial` extension on demand. Air-gapped or network-restricted environments need it pre-installed (see [DuckDB's extension docs](https://duckdb.org/docs/extensions/overview)).
- No wheel is published for Alpine/musl (`python:*-alpine` images) or for glibc <2.26 (e.g. RHEL/CentOS 7, Amazon Linux 2) — these can't get a prebuilt `duckdb` wheel from PyPI. Use a glibc-based image (e.g. `python:3.x-slim`) instead.
- `extend()`/`topo-tools extend` process exactly one file per call by design — loop over files from separate OS processes (a shell loop invoking the CLI, or `subprocess.run` per file from Python), not from within one long-running Python process. See [`docs/performance.md`](docs/performance.md#why-one-file-per-process) for why.

## Supported Formats

Currently, supported inputs are polygons in GeoParquet (.parquet). GeoPackage (.gpkg), Shapefile (.shp), GeoJSON (.geojson) formats. For GeoPackages, all polygon layers inside are processed. Outputs retain their original format, projected to EPSG:4326 (WGS84).

## Usage

### Python

```sh
pip install topo-tools
```

```python
from topo_tools import extend

extend("example.geojson", "example_extended.geojson", memory_gb=4)
```

### CLI

Installing the package also installs a `topo-tools` command:

```sh
topo-tools extend --input-file=example.geojson --output-file=example.geojson
```

Or, without installing, via `python -m`:

```sh
python -m topo_tools extend --input-file=example.geojson --output-file=example.geojson
```

The following options are available. Each also has an environment variable equivalent (useful in containers where flags are awkward to pass):

| Name            | Env Var       | Description                                                                                                        |
| --------------- | ------------- | ------------------------------------------------------------------------------------------------------------------ |
| `--input-file`  | `INPUT_FILE`  | Input file. **Required.**                                                                                          |
| `--output-file` | `OUTPUT_FILE` | Output file. **Required.**                                                                                         |
| `--memory-gb`   | `MEMORY_GB`   | Available memory in GB, used to size point density automatically (default: `4`)                                    |
| `--threads`     | `THREADS`     | DuckDB thread count (default: DuckDB's own default, typically the number of CPU cores)                             |
| `--overwrite`   | `OVERWRITE`   | Overwrite an existing output file (default: `no`)                                                                  |
| `--debug`       | `DEBUG`       | Keep intermediate tables, export them to Parquet, and log detailed timing/memory per query (default: `no`)         |
| `--tmp-dir`     | `TMP_DIR`     | Intermediate DuckDB + Parquet location (default: a fresh temp dir per run, removed after unless `--debug`)         |
| `--step`        | `STEP`        | Run only one pipeline stage — `inputs`, `lines`, `attempt`, `merge`, or `outputs` (default: run the full pipeline) |

Polygons the size of small countries typically take a few seconds, with larger ones at full detail finish in about 10 min. Processing time is proportional to total perimeter length rather than area. The spacing between points on a line is chosen automatically per file, balancing the source data's own level of detail against `--memory-gb` — finer for naturally detailed boundaries, coarser only when needed to fit the memory budget.
