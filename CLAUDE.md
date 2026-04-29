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

# Run the application (--input-file is joined onto input_dir, so pass filename or absolute path;
# --overwrite needed when outputs/<name> already exists regardless of --output-file target)
uv run -m app

# Format and lint
uv run ruff format && uv run ruff check

# Run with Docker
docker build -t edge-extender .
docker run -v .:/srv ghcr.io/fieldmaps/edge-extender --input-file=example.geojson --output-file=example.geojson
```

Pre-commit hooks run `uv-sync`, `ruff-format`, and `ruff-check` automatically.

## Architecture

The pipeline has 6 sequential stages, each a standalone module in `app/`. All stages share a single DuckDB connection (file-backed or in-memory); tables are the IPC mechanism between stages. `__main__.py` chains all stages together in a sequential for-loop over input files.

### Pipeline Stages

1. **`inputs.main`** — Reads geodata via DuckDB `ST_Read`, reprojects to EPSG:4326, stores as `*_01` (geometry)
2. **`clean.main`** — DIY `ST_CoverageClean` equivalent: removes overlaps and absorbs thin-sliver gaps. Rewrites `*_01` in place. Skipped when `ST_CoverageInvalidEdges_Agg` returns NULL/empty (no coverage errors). Lakes (wide interior holes) are preserved. Single swap boundary for the future official `ST_CoverageClean` binding.
3. **`lines.main`** — Extracts boundary lines per polygon; produces `*_02a` (exterior edges) and `*_02b` (interior/shared edges)
4. **`attempt.main`** — Wrapper around `points.main` + `voronoi.main` that retries with doubling distance on failure (0.0002 → 0.1024, up to 10 attempts); `points.main` creates `*_03a` (buffered endpoint union) and `*_03b` (interpolated points), `voronoi.main` generates Voronoi polygons (`*_04`)
5. **`merge.main`** — Merges Voronoi extension with original polygons via `ST_Node` + `ST_Polygonize` (`*_05`)
6. **`outputs.main`** — Validates topology, joins geometry with original attributes, exports via DuckDB COPY

### Configuration

`app/config.py` parses CLI arguments and environment variables at **module level**. All other modules import from config directly. Key settings:

| Setting                    | Default                    | Description                                                   |
| -------------------------- | -------------------------- | ------------------------------------------------------------- |
| `DISTANCE`                 | `0.0002`                   | Point spacing in decimal degrees                              |
| `GAP_MAX_WIDTH`            | `0.0001`                   | Sub-pixel safety net: holes with max-inscribed-diameter ≤ this are absorbed regardless of shape (catches small round artifacts) |
| `GAP_MAX_THINNESS`         | `0.05`                     | Polsby-Popper threshold (`4πA/P²`): primary sliver discriminator. Holes with PP ≤ this are absorbed. 0.05 ≈ 1:30 aspect ratio; intentional shapes (squares ≈ 0.79) easily preserved |
| `OVERLAP_STRATEGY`         | `merge_longest_border`     | Overlap loser selection in clean stage; `merge_longest_border` (PostGIS default) or `largest_area` |
| `INPUT_DIR` / `OUTPUT_DIR` | `../inputs` / `../outputs` | I/O directories (relative to `app/`)                          |
| `TMP_DIR`                  | `../tmp`                   | Intermediate DuckDB + Parquet location                        |
| `THREADS`                  | (unset)                    | DuckDB thread count; unset defers to DuckDB default           |
| `OVERWRITE`                | `False`                    | Overwrite existing output                                     |
| `DEBUG`                    | `False`                    | Keep intermediate tables; export all to Parquet               |
| `PROFILE`                  | `False`                    | Log timing + memory delta per query                           |
| `IN_MEMORY`                | `False`                    | Use in-memory DuckDB instead of file-backed                   |
| `STAGE`                    | (none)                     | Run only one named stage (inputs/clean/lines/attempt/merge/outputs) |

### Table Naming Convention

Tables are named `{name}_{stage}[suffix]` where stage is a two-digit number and suffix is either empty, a letter, or `_tmp{n}`:

- **No suffix** — stage produces exactly one persistent table (e.g. `_01`, `_04`, `_05`)
- **Letter suffix (`_02a`, `_02b`)** — stage produces multiple persistent tables; **all** of them get a letter, including the first. Never leave one bare while siblings have letters.
- **`_tmp{n}` suffix** — table is dropped within the same file before the function returns; not visible to downstream stages unless `--debug` is set

The current sequence: `_01` (potentially rewritten by clean) → `_02a/_02b` → `_03a/_03b` → `_04` → `_05`

### Key Patterns

- **DuckDB spatial extension** handles all geometry operations (`ST_*` functions). One connection (file-backed by default, or in-memory with `--in-memory`) is created per input file in `utils.py` and returned as a `ProfiledConnection` proxy that logs timing and memory per query when `--profile` is set.
- **DuckDB tables as IPC** — stages read and write named tables on the shared connection; no Parquet between stages.
- **Topology validation** in `checks.py`: `check_overlaps`, `check_gaps`, and `check_missing_rows` always run in outputs. All three unnest MultiPolygon geometries before checking to ensure correct coverage validation across individual polygon pieces.
- **Geometry column names**: `geom` in DuckDB tables, `geometry` in final output.
- **Avoid `ST_ClosestPoint(ST_Collect(list(geom)), point)` on large tables.** Collecting thousands of lines into one geometry before calling `ST_ClosestPoint` causes GEOS to allocate large internal acceleration structures — ~6.8 GB for Chile's 700K-point `_02b`. Instead, use a per-segment bbox pre-filter: CROSS JOIN with the source table and filter by `ST_XMin/XMax/YMin/YMax` before calling `ST_ClosestPoint` on only the matching segments. See `_05_tmp2` in `merge.py`.
- **`duckdb_memory()` measurements in isolation underestimate pipeline peaks.** A fresh connection with few tables in the DuckDB file can show 4 GB for a query that peaks at 8 GB in a full pipeline run, because the buffer pool from other large tables (`_01`, `_04`, `_02b`, etc.) adds several GB of additional pressure. Profile with `--stage=X --profile` on a database file that already has all prior-stage tables present.

### Supported Formats

Input/output: GeoParquet (`.parquet`), GeoPackage (`.gpkg`), Shapefile (`.shp`), GeoJSON (`.geojson`). Output format matches input format.

## DuckDB Function Verification

Do not rely on recalled knowledge about DuckDB or spatial extension functions — verify against the installed version before making claims or writing code.

**CLI — best for specific function lookups** (includes full description, parameter docs, return type):

```bash
# Check a specific function — signature + full description
duckdb -c "LOAD spatial; SELECT function_name, parameters, parameter_types, return_type, description FROM duckdb_functions() WHERE function_name ILIKE 'ST_Buffer'"

# List all spatial functions
duckdb -c "LOAD spatial; SELECT function_name, parameters, return_type FROM duckdb_functions() WHERE function_name ILIKE 'ST_%' ORDER BY function_name"

# Search by keyword in description
duckdb -c "LOAD spatial; SELECT function_name, description FROM duckdb_functions() WHERE description ILIKE '%voronoi%'"
```

**gh api — best for browsing the full spatial function reference** (always matched to the installed version):

```bash
# Fetch the full spatial functions reference — branch derived from installed DuckDB version
DUCKDB_REF=$(duckdb --version | sed 's/v\([0-9]*\.[0-9]*\)\.[0-9]* (\([^)]*\)).*/v\1-\2/' | tr '[:upper:]' '[:lower:]') && \
gh api "repos/duckdb/duckdb-spatial/contents/docs/functions.md?ref=${DUCKDB_REF}" --jq '.content' | base64 -d
```

## Reference Docs

- `docs/topology.md` — topology approach (ST_Node + ST_Polygonize), DuckDB spatial function reference, SPATIAL_JOIN memory reservation bug
- `docs/performance.md` — thread-scaling benchmarks, pipeline phase profiles, `get_connection` settings, RTREE experiment
