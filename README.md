# topo-tools

[![CI](https://github.com/fieldmaps/topo-tools-py/actions/workflows/ci.yml/badge.svg)](https://github.com/fieldmaps/topo-tools-py/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/topo-tools)](https://pypi.org/project/topo-tools/)
[![Python versions](https://img.shields.io/pypi/pyversions/topo-tools)](https://pypi.org/project/topo-tools/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![World ADM0 boundaries extended with Voronoi-filled coastline](https://raw.githubusercontent.com/fieldmaps/topo-tools-py/main/img/wld_01.png)

`topo-tools` is a collection of DuckDB-powered geospatial topology utilities. It
ships two tools:

- **extend**: given a layer of polygons with gaps between them (missing coastline,
  disputed areas, water bodies), extends each polygon outward to fill the
  surrounding gaps, producing a complete coverage layer with no overlaps or holes.
  Existing polygon boundaries are left untouched except where they border a gap.
- **match**: fits a child polygon layer into a coarser parent/clip layer (e.g. an
  admin4 layer into a country's admin0 boundary, or into an admin2/admin3 layer
  for per-region grouping). Each child is assigned to the parent it overlaps most,
  extended (via **extend**) within that group to fill any gaps, then clipped to
  its own parent's boundary.

See [`docs/examples.md`](docs/examples.md) for how **extend** works and example
use cases.

## Requirements

- Python 3.10+.
- Network access on first run: DuckDB downloads the `spatial` extension on demand.

## Supported Formats

Currently, supported inputs are polygons in GeoParquet (.parquet). GeoPackage (.gpkg), Shapefile (.shp), GeoJSON (.geojson) formats. For GeoPackages, all polygon layers inside are processed. Outputs retain their original format, projected to EPSG:4326 (WGS84).

## Installation

```sh
pip install topo-tools
```

Installing the package also installs a `topo-tools` command.

## extend

### Python

```python
from topo_tools import extend

extend("example.geojson", "example_extended.geojson", memory_gb=4)
```

### CLI

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

## match

### Python

```python
from topo_tools import match

match("admin4.geojson", "admin0.geojson", "admin4_matched.geojson", memory_gb=4)
```

### CLI

```sh
topo-tools match admin4.geojson admin0.geojson
```

| Name          | Description                                                                     |
| ------------- | ------------------------------------------------------------------------------- |
| `INPUT_FILE`  | Child file to match. **Required.**                                              |
| `CLIP_FILE`   | Parent/clip file. **Required.**                                                 |
| `OUTPUT_FILE` | Output file. Optional -- defaults to `INPUT_FILE` with a `_matched` suffix.     |
| `--memory-gb` | Available memory in GB, used to size point density automatically (default: `4`) |
| `--overwrite` | Overwrite an existing output file (default: `no`)                               |

Run `topo-tools match --help` for the full list. Each parent's group of children
runs in its own isolated subprocess, so a run with many parents (e.g. matching a
nationwide admin4 layer against dozens of admin2 units) scales without one large
group's memory use affecting another's.

### Examples

```sh
# Fit an admin4 layer into a single country boundary
topo-tools match adm4.geojson adm0.geojson

# Fit admin3 into admin2 groups, each cleaned against its own parent
topo-tools match adm3.gpkg adm2.gpkg adm3_matched.gpkg --memory-gb 2
```
