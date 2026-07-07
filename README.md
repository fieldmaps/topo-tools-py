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
- Network access on first run: DuckDB downloads the `spatial` extension on demand.

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
topo-tools extend example.geojson
```

The following options cover the common case:

| Name          | Description                                                                     |
| ------------- | ------------------------------------------------------------------------------- |
| `INPUT_FILE`  | Input file. **Required.**                                                       |
| `OUTPUT_FILE` | Output file. Optional -- defaults to `INPUT_FILE` with an `_extended` suffix.   |
| `--memory-gb` | Available memory in GB, used to size point density automatically (default: `4`) |
| `--overwrite` | Overwrite an existing output file (default: `no`)                               |

Run `topo-tools extend --help` for the full list, including thread count, debug tracing, and running a single pipeline stage.

### Examples

```sh
# Basic run, output name chosen automatically (example.geojson -> example_extended.geojson)
topo-tools extend example.geojson

# Explicit output, sized for a 2GB container
topo-tools extend example.gpkg example_extended.gpkg --memory-gb 2

# Rerun and overwrite a previous output
topo-tools extend example.parquet example_extended.parquet --overwrite
```

Polygons the size of small countries typically take a few seconds, with larger ones at full detail finish in about 10 min. Processing time is proportional to total perimeter length rather than area. The spacing between points on a line is chosen automatically per file, balancing the source data's own level of detail against `--memory-gb` — finer for naturally detailed boundaries, coarser only when needed to fit the memory budget.
