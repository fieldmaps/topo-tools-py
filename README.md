# topo-tools

[![CI](https://github.com/fieldmaps/topo-tools-py/actions/workflows/ci.yml/badge.svg)](https://github.com/fieldmaps/topo-tools-py/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/topo-tools)](https://pypi.org/project/topo-tools/)
[![Python versions](https://img.shields.io/pypi/pyversions/topo-tools)](https://pypi.org/project/topo-tools/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![World ADM0 boundaries extended with Voronoi-filled coastline](https://raw.githubusercontent.com/fieldmaps/topo-tools-py/main/img/wld_01.png)

`topo-tools` is a collection of DuckDB-powered geospatial topology utilities
for cleaning and reconciling administrative boundary polygons. It ships four
tools, usable from the CLI or as a Python package:

| Tool | What it does | Usage |
| --- | --- | --- |
| **clean** | Detects and fixes gap/overlap defects in a single polygon layer; reports slivers separately for manual review. | [`docs/clean.md`](docs/clean.md) |
| **extend** | Fills gaps around a polygon layer (missing coastline, disputed areas, water bodies) with a Voronoi extension, producing full coverage. | [`docs/extend.md`](docs/extend.md) |
| **match** | Fits a finer child polygon layer into a coarser parent layer, grouping and extending each child to fill gaps within its own parent. | [`docs/match.md`](docs/match.md) |
| **change** | Compares two versions of a polygon layer and classifies every unit as unchanged, renamed, modified, split, merged, created, or removed. | [`docs/change.md`](docs/change.md) |

## Installation

```sh
uv tool install topo-tools   # CLI (recommended)
uv add topo-tools            # Python library
```

Or with pip:

```sh
pip install topo-tools
```

Each linked doc above covers that tool's CLI/Python usage, options, and examples.

## Supported Formats

Polygon inputs/outputs: GeoParquet (`.parquet`), GeoPackage (`.gpkg`),
Shapefile (`.shp`), GeoJSON (`.geojson`). Output format matches input format.
`change`'s tabular changelog is CSV or GeoParquet only; its spatial overlay
layer supports the same four formats as the other tools.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup.
