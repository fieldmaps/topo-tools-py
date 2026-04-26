# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Overview

Edge Extender is a geospatial tool that extends polygon boundaries outward using Voronoi diagrams, producing a complete coverage layer that fills gaps (e.g., coastlines, disputed areas, water bodies). It is used for matching sub-national boundaries to national boundaries and improving administrative boundary datasets.

## Deployment Targets

The pipeline is designed for two memory-constrained environments:

1. **DuckDB-WASM in the browser** — no disk, JavaScript heap only; the Python pipeline logic documents the SQL approach for eventual JS/TS porting
2. **Memory-limited Docker containers** — typically 2–4 GB RAM, no swap

Memory efficiency is a first-class concern. Prefer approaches that minimize intermediate materializations, avoid platform-specific calls (`os.sysconf`, `/proc`, `subprocess`), and work with small buffer budgets.

## Test Datasets

| Dataset | Use |
|---|---|
| **Burundi** | Small, fast — good for quick iteration |
| **Chile** | Large coastline, most memory-intensive — the canonical stress test |

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

| Setting                    | Default        | Description                      |
| -------------------------- | -------------- | -------------------------------- |
| `DISTANCE`                 | `0.0002`       | Point spacing in decimal degrees |
| `INPUT_DIR` / `OUTPUT_DIR` | `.`            | I/O directories                  |
| `TMP_DIR`                  | same as output | Intermediate DuckDB + Parquet location |
| `THREADS`                  | `4`            | DuckDB thread count per connection |
| `OVERWRITE`                | `False`        | Overwrite existing output        |

### Key Patterns

- **DuckDB spatial extension** handles all geometry operations (`ST_*` functions). One file-backed connection is created per input file in `utils.py`.
- **DuckDB tables as IPC** — stages read and write named tables on the shared connection; no Parquet between stages.
- **Topology validation** in `topology.py` (`check_overlaps`, `check_gaps`, `check_missing_rows`) runs after Voronoi generation and again before final output.
- **Geometry column names**: `geom` in DuckDB tables, `geometry` in final output.

### Supported Formats

Input/output: GeoParquet (`.parquet`), GeoPackage (`.gpkg`), Shapefile (`.shp`), GeoJSON (`.geojson`). Output format matches input format.

## Topology: DuckDB vs `gdal vector clean-coverage`

The pipeline previously called `gdal vector clean-coverage` (GEOS `GEOSCoverageSimplify`/repair) at the inputs and merge stages. It has been removed. This section records what DuckDB can and cannot replicate, and the approach that was chosen.

### What DuckDB spatial exposes

| Function | Purpose |
|---|---|
| `ST_CoverageInvalidEdges_Agg` | Detects edges that don't match between adjacent polygons (validation only, no repair) |
| `ST_CoverageSimplify_Agg` | Topology-safe simplification (does not fix gaps or overlaps) |
| `ST_CoverageUnion_Agg` | Fast union for already-valid coverages (crashes on invalid input) |
| `ST_ReducePrecision` | Snaps vertices to a grid — makes edge mismatch worse when applied to only one layer |
| `ST_Node` | Computes all intersection points between a collection of lines, adding them as shared vertices |
| `ST_Polygonize` | Builds polygons from a planar noded edge network |
| `ST_MemUnion_Agg` | Memory-efficient union aggregate |

There is **no `ST_CoverageClean` or `ST_Snap`**. The GEOS coverage repair functions are not exposed.

### Why the naive approach creates gaps

The previous merge used `ST_Difference(voronoi_cell, ST_Union_Agg(nearby_originals))` per cell. This recomputed the original polygon boundary independently for each Voronoi cell. GEOS floating-point arithmetic produces slightly different crossing-point coordinates each time, creating sub-nanometer seam gaps that appear as visible diagonal lines in QGIS.

Applying `ST_ReducePrecision` to only the extension pieces (not originals) makes the problem **worse**: it snaps extension vertices to a grid that doesn't align with the original polygon coordinates, increasing mismatches.

### The solution: `ST_Node` + `ST_Polygonize`

`merge.main` now:

1. Collects **all original polygon boundaries** (`ST_Boundary` of `_01`) and **all Voronoi cell boundaries** (`ST_Boundary` of `_04`) into one edge set.
2. Calls `ST_Node` on the combined edge set — every crossing point (where a Voronoi boundary crosses an original polygon edge) becomes a shared vertex in both geometries simultaneously. No crossing point is ever computed twice.
3. Calls `ST_Polygonize` on the noded edges — produces a clean planar partition of the entire extent with no gaps or overlaps.
4. Assigns each piece to a `fid` via `ST_PointOnSurface` + point-in-polygon: original polygon assignment takes priority (preserving authoritative boundaries exactly), complement pieces fall back to the enclosing Voronoi cell.
5. Unions pieces by `fid`.

This produces **0 gaps, 0 overlaps, 0 `ST_CoverageInvalidEdges`** on all tested datasets. Original polygon vertex coordinates are never modified — the noding only adds collinear intermediate vertices where Voronoi edges cross original polygon edges, which is geometrically identical.

### Topology checks (`topology.py`)

Both a **strict** and an **area-based** check are run in parallel and compared:

| Check | Strict | Area-based (authoritative) |
|---|---|---|
| Overlaps | `ST_CoverageInvalidEdges_Agg IS NOT NULL` | `ST_Area(ST_Intersection) > 1e-10` |
| Gaps | `ST_NumInteriorRings(ST_Union_Agg) > 0` | `ST_Area(ST_Difference(extent, union)) > 1e-10` |

When the two disagree, a `WARNING` is logged with both values. The run only fails on the area-based result. `AREA_EPSILON = 1e-10` (≈ 0.1 m²) is the threshold below which a discrepancy is treated as a floating-point artifact rather than a real topology error.

## DuckDB 1.5.2 `SPATIAL_JOIN` Memory Reservation Bug

DuckDB 1.5.2's `SPATIAL_JOIN` operator pre-allocates approximately **1× physical RAM** as a virtual memory spill reservation before executing, regardless of actual data size. The default `memory_limit` of 80% RAM falls below this threshold on most machines, causing an immediate OOM error even when the join touches only ~100 MB of real data.

**Symptom**: The OOM message reads `"failed to allocate data of size X MiB (Y GiB/Y GiB used)"` where Y equals the `memory_limit` exactly. `duckdb_memory().memory_usage_bytes` shows only 60–100 MB — the two tracking systems are independent. The budget is exhausted by the reservation, not real data.

**What triggers `SPATIAL_JOIN`**: Any `ST_Within` / `ST_Contains` predicate in a JOIN. DuckDB's optimizer always rewrites to `SPATIAL_JOIN` — correlated subqueries, `LATERAL` joins, and batching all produce the same plan.

**Workaround** (implemented in `merge.main`): Temporarily raise `memory_limit` to 1.5× physical RAM around the spatial joins, then restore the original value. The virtual reservation is cheap for the OS (no physical pages allocated); raising the limit passes the reservation check without real memory pressure.

```python
_orig_limit = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
try:
    _sys_b = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    conn.execute(f"SET memory_limit = '{int(_sys_b * 1.5)}B'")
except (AttributeError, ValueError):
    conn.execute("SET memory_limit = '24GB'")
# ... spatial joins ...
conn.execute(f"SET memory_limit = '{_orig_limit}'")
```

**Threshold on a 16 GiB machine**: Reservation is ~16.7 GiB. Fails at limits ≤ 18 GB, passes at 19 GB. 1.5× RAM (24 GiB) provides comfortable headroom on all tested machines.

**Note**: R-tree indexes (`CREATE INDEX ... USING RTREE (geom)`) are required before the spatial joins for efficient probing. May be fixed in DuckDB versions after 1.5.2 — re-test if upgrading.
