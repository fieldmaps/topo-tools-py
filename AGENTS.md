# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Overview

Edge Extender is a geospatial tool that extends polygon boundaries outward using Voronoi diagrams, producing a complete coverage layer that fills gaps (e.g., coastlines, disputed areas, water bodies). It is used for matching sub-national boundaries to national boundaries and improving administrative boundary datasets.

## Commands

```bash
# Install dependencies
uv sync

# Run the application
uv run -m app
task app

# Format and lint
task ruff   # runs: ruff format && ruff check

# Run with Docker
docker build -t edge-extender .
docker run -v .:/srv ghcr.io/fieldmaps/edge-extender --input-file=example.geojson --output-file=example.geojson
```

Pre-commit hooks run `uv-sync`, `ruff-format`, and `ruff-check` automatically.

## Architecture

The pipeline has 6 sequential stages, each a standalone module in `app/`. All stages share a single DuckDB in-memory connection and exchange data via compressed Parquet files (ZSTD level 15). `utils.apply_funcs()` chains all stages together.

### Pipeline Stages

1. **`inputs.main`** — Imports geodata via GDAL, reprojects to EPSG:4326, snaps topology with `coverage_clean`, stores as Parquet (`*_attr`, `*_01`)
2. **`lines.main`** — Extracts polygon boundary lines (`ST_Boundary`), unions them, then intersects to retain per-polygon attributes (`*_02`)
3. **`attempt.main`** — Wrapper around `points.main` + `voronoi.main` that retries with doubling distance on failure (0.0002 → 0.1024, up to 10 attempts)
4. **`points.main`** — Creates interpolated points along boundary lines at configurable intervals; excludes endpoints (`*_03`)
5. **`voronoi.main`** — Generates Voronoi polygons from points (`ST_VoronoiDiagram`), clips to bounding extent (`*_04`)
6. **`merge.main`** — Unions Voronoi extension outside original coverage with original polygons, applies `coverage_clean` (`*_05`)
7. **`outputs.main`** — Joins geometry with original attributes, validates topology, exports via GDAL (up to 5 retries with backoff)
8. **`cleanup.main`** — Deletes all intermediate Parquet files

### Parallelism

`__main__.py` discovers input files, filters for polygon geometries (`is_polygon()`), and processes each file/layer in parallel via `multiprocessing.Pool`. GeoPackages with multiple polygon layers are each processed independently.

### Configuration

`app/config.py` parses CLI arguments and environment variables at **module level**. All other modules import from config directly. Key settings:

| Setting                    | Default        | Description                      |
| -------------------------- | -------------- | -------------------------------- |
| `DISTANCE`                 | `0.0002`       | Point spacing in decimal degrees |
| `INPUT_DIR` / `OUTPUT_DIR` | `.`            | I/O directories                  |
| `TMP_DIR`                  | same as output | Intermediate Parquet location    |
| `NUM_THREADS`              | `cpu_count()`  | Parallel workers                 |
| `OVERWRITE`                | `False`        | Overwrite existing output        |

### Key Patterns

- **DuckDB spatial extension** handles all geometry operations (`ST_*` functions). Connections are created per-process in `utils.py`.
- **GDAL CLI** (`ogr2ogr`) is used for format I/O only — no geometry processing.
- **Parquet as IPC** — intermediate stages write/read Parquet; no in-memory passing between stages.
- **Topology validation** in `topology.py` (`check_overlaps`, `check_gaps`, `check_missing_rows`) runs after merge before final output.
- **Geometry column names**: `geom` in intermediate Parquet, `geometry` in final output.

### Supported Formats

Input/output: GeoParquet (`.parquet`), GeoPackage (`.gpkg`), Shapefile (`.shp`), GeoJSON (`.geojson`). Output format matches input format.
