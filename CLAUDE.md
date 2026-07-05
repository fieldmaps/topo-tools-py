# CLAUDE.md

## Verification Over Recall

- Never rely on remembered knowledge for libraries, APIs, or frameworks —
  check installed versions and docs before writing code or making claims
- If you lack verified information, acknowledge uncertainty and investigate
  first rather than speculate

## Collaboration Style

- Be objective, not agreeable — act as a partner, not a sycophant
- Push back when you disagree, flag tradeoffs honestly, don't sugarcoat problems
- Keep explanations brief and to the point
- Accuracy over speed
- Code comments: terse, max 1–2 lines, only when the WHY is non-obvious; never restate what the code does

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

## Batch Processing Many Files

Multi-file runs (`uv run -m app` over a whole `input_dir`) isolate each file in its
own subprocess (`__main__._run_isolated`, dispatching `uv run -m app --input-file=X`
per file) rather than looping over files inside one long-lived process. This was a
deliberate fix, not the original design: looping in-process let RSS grow unbounded
across files — confirmed empirically at ~2GB after a few files up to 19.85GB after
~25 minutes / ~10 files, exceeding the machine's physical RAM and forcing heavy swap,
even though every individual file peaked under 3GB when profiled in isolation.
`conn.close()` per file did not prevent this: GEOS's native heap (used by
`ST_VoronoiDiagram`, `ST_Union_Agg`, coverage-clean) allocates outside DuckDB's own
buffer-pool tracking, so it wasn't guaranteed to be returned to the OS between files
even with each file's DuckDB connection properly closed. Don't revert to an in-process
loop over `input_dir` — that reintroduces the leak. `--threads` count is unrelated to
this mechanism — it affects per-query peak, not cross-file retention.

`--input-file` single-file invocations (the subprocess's own recursive base case)
still run in-process directly — there's nothing to isolate for a single file.

Reminder when scripting this: `--input-file` is joined onto `input_dir` unless
absolute, so pass the bare filename (`mex_admin2_v02.parquet`), not a path that
already includes the `inputs/` prefix (e.g. from a `inputs/*.parquet` glob) — that
double-joins to a nonexistent path, and the pipeline silently no-ops (no error, no
output) rather than failing loudly.

## Architecture

The pipeline has 5 sequential stages, each a standalone module in `app/`. All stages share a single file-backed DuckDB connection; tables are the IPC mechanism between stages. `__main__.py` chains all stages together in a sequential for-loop over input files.

### Pipeline Stages

1. **`inputs.main`** — Reads geodata via DuckDB `ST_Read`, reprojects to EPSG:4326, stores as `*_01` (geometry). Then pre-checks `_01` with `ST_CoverageInvalidEdges_Agg`; if it finds invalid edges, runs `ST_CoverageClean` over the whole table and rewrites `*_01` in place. No-op otherwise. Requires DuckDB spatial ≥ 1.5.3 for native `ST_CoverageClean`. No memory-budget check here — `ST_CoverageClean`'s cost scales with the file's own raw vertex count with no resampling lever to shrink it (`phl_admin3`, 13.85M vertices, needs ~5.9GB and won't fit a 4GB deployment — see `docs/voronoi-memory.md`); `--memory-gb` is a soft target for `attempt.py`'s DISTANCE selection, not a hard gate here. Does not distinguish real holes from digitization slivers — inputs are expected to be pre-cleaned upstream; any narrow gap that slips through is treated the same as a real hole and left for `merge.main`'s Voronoi extension to divide. Byte-exact preservation of untouched polygons is not a goal — see Key Patterns.
2. **`lines.main`** — Extracts each polygon's exterior boundary (its own boundary minus a bbox-prefiltered union of its neighbors' boundaries); produces `*_02`. Same caveat as `inputs.main`: the neighbor-union self-join's cost scales with `_01`'s raw vertex count with no resampling lever (`idn_admin3`, 7.48M vertices, needs ~5.4GB — see `docs/voronoi-memory.md`); no runtime memory check here.
3. **`attempt.main`** — Wrapper around `points.main` + `voronoi.main` that retries with doubling distance on failure (0.0002 → 0.1024, up to 10 attempts); `points.main` creates `*_03a` (buffered endpoint union) and `*_03b` (interpolated points), `voronoi.main` generates Voronoi polygons (`*_04`)
4. **`merge.main`** — Unions each fid's original geometry with its Voronoi extension (`*_04`) minus a bbox-prefiltered union of nearby originals, then runs a single whole-table `ST_CoverageClean` pass to close floating-point-scale seams (`*_05`)
5. **`outputs.main`** — Validates topology and exports via DuckDB COPY

### Configuration

`app/config.py` parses CLI arguments and environment variables at **module level**. All other modules import from config directly. Key settings:

| Setting                    | Default                    | Description                                                         |
| -------------------------- | -------------------------- | ------------------------------------------------------------------- |
| `DISTANCE`                 | `0.0002`                   | Point spacing in decimal degrees                                    |
| `INPUT_DIR` / `OUTPUT_DIR` | `../inputs` / `../outputs` | I/O directories (relative to `app/`)                                |
| `TMP_DIR`                  | `../tmp`                   | Intermediate DuckDB + Parquet location                              |
| `THREADS`                  | (unset)                    | DuckDB thread count; unset defers to DuckDB default                 |
| `MEMORY_GB`                | `4`                        | Available memory in GB; derives attempt.py's per-file DISTANCE point budget (see `docs/voronoi-memory.md`) — set to the real container/deployment limit |
| `OVERWRITE`                | `False`                    | Overwrite existing output                                           |
| `DEBUG`                    | `False`                    | Keep intermediate tables, export all to Parquet, and log timing + memory delta per query |
| `STEP`                     | (none)                     | Run only one named stage (inputs/lines/attempt/merge/outputs)       |

### Table Naming Convention

Tables are named `{name}_{stage}[suffix]` where stage is a two-digit number and suffix is either empty, a letter, or `_tmp{n}`:

- **No suffix** — stage produces exactly one persistent table (e.g. `_01`, `_04`, `_05`)
- **Letter suffix (`_03a`, `_03b`)** — stage produces multiple persistent tables; **all** of them get a letter, including the first. Never leave one bare while siblings have letters.
- **`_tmp{n}` suffix** — table is dropped within the same file before the function returns; not visible to downstream stages unless `--debug` is set

The current sequence: `_01` → `_02` → `_03a/_03b` → `_04` → `_05`. `inputs.main`'s coverage-clean pass rewrites `_01` in place when violations are detected; it does not introduce a new suffix.

### Key Patterns

- **DuckDB spatial extension** handles all geometry operations (`ST_*` functions). One file-backed connection is created per input file in `utils.py` and returned as a `ProfiledConnection` proxy that logs timing and memory per query when `--debug` is set.
- **DuckDB tables as IPC** — stages read and write named tables on the shared connection; no Parquet between stages.
- **Topology validation** in `_06_outputs.py` (`_check_overlaps`, `_check_gaps`) always runs in outputs, backed by `has_coverage_violations` in `utils.py`. Both unnest MultiPolygon geometries before checking to ensure correct coverage validation across individual polygon pieces. There is no byte-exactness check — see below.
- **Geometry column names**: `geom` in DuckDB tables, `geometry` in final output.
- **`duckdb_memory()` measurements in isolation underestimate pipeline peaks.** A fresh connection with few tables in the DuckDB file can show 4 GB for a query that peaks at 8 GB in a full pipeline run, because the buffer pool from other large tables (`_01`, `_04`, `_05_tmp1`, etc.) adds several GB of additional pressure. Profile with `--step=X --debug` on a database file that already has all prior-stage tables present.
- **Avoid materializing one global `ST_Union_Agg` of `_01` as a per-row `ST_Difference`/join operand.** At Chile scale the union can hold millions of vertices; using it as an operand against every fid individually made GEOS pay that cost on every row and OOM'd outright (confirmed during development of `_05_merge.py`). Use a bbox-prefiltered join against nearby originals instead (see `_05_merge.py`'s `_05_tmp1`/`_05_tmp2`, which explodes multipolygon fids into parts first — a whole-fid bbox can span mainland-to-remote-island and defeat the prefilter). **`_02_lines.py`'s neighbor-union self-join deliberately does NOT do this** — it joins on whole-fid bboxes. Exploding it into per-part bboxes looks like the same fix but isn't: it helps files with many fids that each have a few widely-scattered parts (e.g. `idn_admin3`) but badly regresses files with one fid made of thousands of tightly-clustered parts (e.g. `chl_admin3` has a single fid with 3,796 parts) by multiplying self-join row count far more than the tighter bboxes save — confirmed empirically (Chile: 3.3GB peak with whole-fid bboxes vs. OOM at 10GB+ with per-part bboxes). See `docs/voronoi-memory.md`.
- **Byte-exact preservation of original polygon vertices is not a goal.** `ST_CoverageClean` may shift any polygon's boundary, including previously-untouched ones. Don't reintroduce per-fid violator scoping, snapshot/restore, or escalation logic to protect vertex-level exactness — that machinery was removed deliberately (see `docs/topology.md`).

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
- `docs/voronoi-memory.md` — Voronoi collinearity degeneracy fix (segment cap, dynamic DISTANCE), `--memory-gb`-derived point budget fitted inside a real memory-limited Docker container, and two still-open OOM bottlenecks in `inputs.py`/`lines.py`
