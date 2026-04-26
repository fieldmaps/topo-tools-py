# CLAUDE.md

This file provides guidance to agents when working with code in this repository.

## Project Overview

Edge Extender is a geospatial tool that extends polygon boundaries outward using Voronoi diagrams, producing a complete coverage layer that fills gaps (e.g., coastlines, disputed areas, water bodies). It is used for matching sub-national boundaries to national boundaries and improving administrative boundary datasets.

## Deployment Targets

The pipeline is designed for two memory-constrained environments:

1. **DuckDB-WASM in the browser** — no disk, JavaScript heap only; the Python pipeline logic documents the SQL approach for eventual JS/TS porting
2. **Memory-limited Docker containers** — typically 2–4 GB RAM, no swap

Memory efficiency is a first-class concern. Prefer approaches that minimize intermediate materializations, avoid platform-specific calls (`os.sysconf`, `/proc`, `subprocess`), and work with small buffer budgets.

## Test Datasets

| Dataset                            | Use                                                                |
| ---------------------------------- | ------------------------------------------------------------------ |
| **Burundi** (`bdi_admin2.parquet`) | Small, fast — good for quick iteration                             |
| **Chile** (`chl_admin3.parquet`)   | Large coastline, most memory-intensive — the canonical stress test |

## Commands

```bash
# Install dependencies
uv sync

# Run the application
uv run -m app

# Format and lint
uv run ruff format && uv run ruff check

# Run with Docker
docker build -t edge-extender .
docker run -v .:/srv ghcr.io/fieldmaps/edge-extender --input-file=example.geojson --output-file=example.geojson
```

Pre-commit hooks run `uv-sync`, `ruff-format`, and `ruff-check` automatically.

## Architecture

The pipeline has 6 sequential stages, each a standalone module in `app/`. All stages share a single file-backed DuckDB connection; tables are the IPC mechanism between stages. `__main__.py` chains all stages together in a sequential for-loop over input files.

### Pipeline Stages

1. **`inputs.main`** — Reads geodata via DuckDB `ST_Read`, reprojects to EPSG:4326, stores as `*_attr` (attributes) and `*_01` (geometry)
2. **`lines.main`** — Extracts exterior boundary lines per polygon via lateral-join neighbour union (`*_02`)
3. **`attempt.main`** — Wrapper around `points.main` + `voronoi.main` that retries with doubling distance on failure (0.0002 → 0.1024, up to 10 attempts)
4. **`points.main`** — Creates interpolated points along exterior boundary lines at configurable intervals (`*_03`)
5. **`voronoi.main`** — Generates Voronoi polygons from points (`ST_VoronoiDiagram`), assigns fid, validates topology (`*_04`)
6. **`merge.main`** — Merges Voronoi extension with original polygons via `ST_Node` + `ST_Polygonize` (`*_05`)
7. **`outputs.main`** — Validates topology, joins geometry with original attributes, exports via DuckDB COPY

### Configuration

`app/config.py` parses CLI arguments and environment variables at **module level**. All other modules import from config directly. Key settings:

| Setting                    | Default        | Description                            |
| -------------------------- | -------------- | -------------------------------------- |
| `DISTANCE`                 | `0.0002`       | Point spacing in decimal degrees       |
| `INPUT_DIR` / `OUTPUT_DIR` | `.`            | I/O directories                        |
| `TMP_DIR`                  | same as output | Intermediate DuckDB + Parquet location |
| `THREADS`                  | `4`            | DuckDB thread count per connection     |
| `OVERWRITE`                | `False`        | Overwrite existing output              |

### Key Patterns

- **DuckDB spatial extension** handles all geometry operations (`ST_*` functions). One file-backed connection is created per input file in `utils.py`.
- **DuckDB tables as IPC** — stages read and write named tables on the shared connection; no Parquet between stages.
- **Topology validation** in `topology.py` (`check_overlaps`, `check_gaps`, `check_missing_rows`) runs after Voronoi generation and again before final output.
- **Geometry column names**: `geom` in DuckDB tables, `geometry` in final output.

### Supported Formats

Input/output: GeoParquet (`.parquet`), GeoPackage (`.gpkg`), Shapefile (`.shp`), GeoJSON (`.geojson`). Output format matches input format.

## Reference Docs

- `docs/topology.md` — topology approach (ST_Node + ST_Polygonize), DuckDB spatial function reference, SPATIAL_JOIN memory reservation bug
- `docs/performance.md` — thread-scaling benchmarks, pipeline phase profiles, `get_connection` settings, RTREE experiment
