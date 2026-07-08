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

`topo-tools` is a Python package of DuckDB-powered geospatial topology utilities,
`pip install`-able and importable, mirroring the organization of the sister JS app
at `../topo-tools` (a DuckDB-WASM web app with the same tools). It ships three
tools:

- **extend**: extends polygon boundaries outward using Voronoi diagrams,
  producing a complete coverage layer that fills gaps (e.g., coastlines,
  disputed areas, water bodies).
- **match**: fits a child polygon layer into a coarser parent/clip layer
  (e.g. admin4 into admin0) by assigning each child to the parent it shares
  the largest area with, grouping children by that assignment, running
  `extend`'s pipeline within each group, and clipping each group's result to
  its own parent. `core.match` depends on `core.extend`; the reverse
  dependency is forbidden by an import-linter contract (see Key Patterns).
- **clean**: detects and fixes coverage defects (gaps, overlaps) in a single
  polygon layer with `ST_CoverageClean`; detects but never auto-fixes
  slivers (near-miss boundary mismatches), reporting them in a separate
  issues file alongside the cleaned dataset for manual review. `core.clean`
  depends on `core.extend`; the reverse dependency is forbidden by the same
  kind of import-linter contract as `match`. See `docs/cleaning.md`.

All three are used for improving administrative boundary datasets and
matching sub-national boundaries to national boundaries.

## Deployment Targets

The pipeline is designed for two memory-constrained environments:

1. **DuckDB-WASM in the browser** — no disk, JavaScript heap only; the Python pipeline logic documents the SQL approach for eventual JS/TS porting
2. **Memory-limited containers** — typically 2–4 GB RAM, no swap; this repo doesn't ship a Dockerfile itself (pip-install this package into whatever container image you need), but the memory model still targets that class of deployment

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

# Run the extend tool (processes exactly one file per call)
uv run topo-tools extend example.geojson
# equivalently: uv run python -m topo_tools extend example.geojson

# Run the match tool (fits a child layer into a parent/clip layer)
uv run topo-tools match children.geojson parents.geojson

# Run the clean tool (detects/fixes gaps+overlaps, reports slivers separately)
uv run topo-tools clean example.geojson

# Format and lint
uv run ruff format && uv run ruff check
```

Pre-commit hooks run `uv-sync`, `ruff-format`, and `ruff-check` automatically.

## Architecture

Each tool's pipeline is a sequence of stages, each a standalone module in its
own `topo_tools/core/{tool}/` package. All stages of one `extend()`/`match()`
call share a single file-backed DuckDB connection; tables are the IPC
mechanism between stages (per-group subprocesses inside `match` are the one
exception — see `docs/matching.md`). Three layers, each with a specific job
(mirroring `geoparquet-io`'s `core`/`api`/`cli` split — see ADRs
`0001-cli-core-separation`/`0004-python-api-mirrors-cli` in that repo):

- `topo_tools/core/extend/`, `topo_tools/core/match/`, `topo_tools/core/clean/`
  — the real stage implementations. No `click` import. `core.match` and
  `core.clean` may each import from `core.extend` (both reuse `extend`'s
  stage functions/helpers); the reverse is forbidden by import-linter
  contracts (`pyproject.toml`).
- `topo_tools/api/extend.py`, `topo_tools/api/match.py`, `topo_tools/api/clean.py`
  — the public `extend()`/`match()`/`clean()` functions; each chains its own
  tool's stages together for exactly one file (or one child file + one clip
  file) per call. No `click` import.
- `topo_tools/cli/main.py` — the click CLI; maps flags/env vars onto a single
  `api.extend()`/`api.match()`/`api.clean()` call per invocation. Processes
  exactly one file (or file pair) per invocation — no directory batching.
  The only layer allowed to import `click`.

### Pipeline Stages

1. **`inputs.main`** — Reads geodata via DuckDB `ST_Read`, reprojects to EPSG:4326, stores as `*_01` (geometry). Then pre-checks `_01` with `ST_CoverageInvalidEdges_Agg`; if it finds invalid edges, runs `ST_CoverageClean` over the whole table and rewrites `*_01` in place. No-op otherwise. Requires DuckDB spatial ≥ 1.5.3 for native `ST_CoverageClean`. No memory-budget check here — `ST_CoverageClean`'s cost scales with the file's own raw vertex count with no resampling lever to shrink it (`phl_admin3`, 13.85M vertices, needs ~5.9GB and won't fit a 4GB deployment — see `docs/voronoi-memory.md`); `--memory-gb` is a soft target for `attempt.py`'s DISTANCE selection, not a hard gate here. Does not distinguish real holes from digitization slivers — inputs are expected to be pre-cleaned upstream; any narrow gap that slips through is treated the same as a real hole and left for `merge.main`'s Voronoi extension to divide. Byte-exact preservation of untouched polygons is not a goal — see Key Patterns.
2. **`lines.main`** — Extracts each polygon's exterior boundary (its own boundary minus a bbox-prefiltered union of its neighbors' boundaries); produces `*_02`. Same caveat as `inputs.main`: the neighbor-union self-join's cost scales with `_01`'s raw vertex count with no resampling lever (`idn_admin3`, 7.48M vertices, needs ~5.4GB — see `docs/voronoi-memory.md`); no runtime memory check here.
3. **`attempt.main`** — Wrapper around `points.main` + `voronoi.main` that retries with doubling distance on failure (0.0002 → 0.1024, up to 10 attempts); `points.main` creates `*_03a` (buffered endpoint union) and `*_03b` (interpolated points), `voronoi.main` generates Voronoi polygons (`*_04`)
4. **`merge.main`** — Unions each fid's original geometry with its Voronoi extension (`*_04`) minus a bbox-prefiltered union of nearby originals, then runs a single whole-table `ST_CoverageClean` pass to close floating-point-scale seams (`*_05`)
5. **`outputs.main`** — Validates topology and exports via DuckDB COPY

### Match Pipeline Stages

1. **`_01_inputs.main`** — Loads and coverage-cleans both the child and
   parent/clip layers by delegating twice to `extend`'s own `_01_inputs.main`
   (`{name}_child_01`, `{name}_parent_01`).
2. **`_02_assign.main`** — Assigns each child to the parent it shares the
   largest area with (bbox-prefiltered, part-exploded, ranked in EPSG:8857);
   drops and logs children with zero overlap with any parent
   (`{name}_02_pairs`, `{name}_02_assign`, `{name}_02_unassigned`).
3. **`_03_groups.main`** — Groups children by assigned parent (always, even a
   group of one); for each group, exports its children + parent geometry to
   Parquet, runs `extend`'s `_02_lines`/`attempt`/`_05_merge` stage functions
   in an isolated `multiprocessing` (`spawn`) subprocess, clips the result to
   that group's parent, appends survivors into `{name}_03`. A failed group is
   logged and dropped, not fatal — `match()` only raises if no group produces
   any output at all. See `docs/matching.md` for the full rationale.
4. **`_04_merge.main`** — Single whole-table `ST_CoverageClean` pass over
   `{name}_03` to close cross-group seams (`{name}_04`).
5. **`_05_outputs.main`** — Validates topology (reusing `extend`'s
   `check_overlaps`/`check_gaps`, hoisted into `core/extend/_coverage.py` as
   public functions) and exports via DuckDB COPY.

### Clean Pipeline Stages

1. **`_01_inputs.main`** — Reads and reprojects via `extend`'s
   `read_and_reproject()` helper, **without** `extend`'s own auto-clean
   pre-check — `clean`'s detection stage needs to see the raw, uncleaned
   input (`{name}_01`).
2. **`_02_issues.main`** — Detects gap/overlap/sliver defects, writing one
   issues table (`{name}_02`: `key`, `kind`, `area_m2`, `max_width_m`,
   `unit_a`, `unit_b`, `geom` — mixed Polygon/LineString geometry). Gaps only
   catch fully-enclosed holes; overlaps are bbox-prefiltered pairwise
   intersections; slivers are `ST_CoverageInvalidEdges_Agg` near-misses with
   already-detected gap/overlap regions subtracted. See `docs/cleaning.md`.
3. **`_03_clean.main`** — Fixes gaps/overlaps via `extend`'s
   `coverage_clean()` (gated: a no-op copy if the input has no coverage
   violations at all), writing `{name}_03`. Slivers are never touched.
4. **`_04_outputs.main`** — Validates overlaps are gone (`check_overlaps`,
   hard gate); logs (does not raise on) any gaps left unfilled by design;
   exports both the cleaned dataset and the issues report. Does **not**
   reuse `check_gaps` as a hard gate — unlike `extend`/`match`, `clean` can
   legitimately leave gaps unfilled.

### Configuration

No module-level `argparse`/env parsing anywhere — that pattern used to live in
`app/config.py` and broke `import topo_tools` (parsing the host process's `sys.argv`
as a side effect of importing). Settings now flow in two ways:

- **User-configurable, varies per call** — plain keyword arguments on
  `topo_tools.api.extend.extend()`, threaded explicitly into exactly the stage
  functions that read them (confirmed by reading every stage: `_01_inputs`/`_02_lines`
  need nothing; `_03_points`/`_04_voronoi`/`_05_merge` need `debug`; `attempt` needs
  `memory_gb` + `debug`; `_06_outputs` needs `debug`; `get_connection` needs
  `threads` + `debug`). `topo_tools/cli/main.py`'s `extend` command maps CLI
  args/flags/env vars 1:1 onto these kwargs (env var names match the old
  `config.py` ones — `INPUT_FILE`, `MEMORY_GB`, `DEBUG`, etc. — via click's
  `envvar=`; `INPUT_FILE`/`OUTPUT_FILE` are positional `click.argument`s,
  everything else is a `click.option`).
- **Not user-configurable, pure literals** — `topo_tools/core/extend/_constants.py`
  (`MAX_POINTS`, `SNAP_TOLERANCE`, `DEFAULT_DISTANCE`,
  `MAX_POINTS_PER_SEGMENT`, the memory-model constants, `COPY_OPTS`). Safe to import
  at module load — no argparse, no env reads.

| Setting                    | Description                                                         |
| -------------------------- | ------------------------------------------------------------------- |
| `input_path` / `output_path` | Input/output file paths (one file per call); `output_path` defaults to `input_path` with an `_extended` suffix when omitted |
| `tmp_dir`                  | Intermediate DuckDB + Parquet location; defaults to a fresh `tempfile.mkdtemp()` when unset, cleaned up after the call unless `debug` |
| `threads`                  | DuckDB thread count; unset defers to DuckDB default                 |
| `memory_gb`                | Available memory in GB; derives attempt.py's per-file resampling distance/point budget (see `docs/voronoi-memory.md`) — set to the real container/deployment limit |
| `overwrite`                | Overwrite existing output                                           |
| `debug`                    | Keep intermediate tables, export all to Parquet, and log timing + memory delta per query |
| `step`                     | Run only one named stage (inputs/lines/attempt/merge/outputs)       |

`topo_tools.api.match.match()` takes the same settings plus a required
`clip_path` (the parent/clip layer, positional between `input_path` and
`output_path`); `output_path` defaults to an `_matched` suffix instead of
`_extended`, and `step` chooses among `inputs/assign/groups/merge/outputs`.

`topo_tools.api.clean.clean()` takes `input_path`, optional `output_path`
(`_cleaned` suffix) and optional `issues_path` (`_issues` suffix, derived
from `output_path`'s stem), plus `gap_width` (`"auto"`/`"all"`/a meters
string, default `"all"`), `snap_tolerance` (`"auto"`/a meters string,
default `"auto"`), `sliver_tolerance_m` (default `10.0`), and the same
`threads`/`tmp_dir`/`overwrite`/`debug` settings; `step` chooses among
`inputs/issues/clean/outputs`. No `memory_gb` — `clean` has no Voronoi stage
to size a resampling budget for.

### Table Naming Convention

Tables are named `{name}_{stage}[suffix]` where stage is a two-digit number and suffix is either empty, a letter, or `_tmp{n}`:

- **No suffix** — stage produces exactly one persistent table (e.g. `_01`, `_04`, `_05`)
- **Letter suffix (`_03a`, `_03b`)** — stage produces multiple persistent tables; **all** of them get a letter, including the first. Never leave one bare while siblings have letters.
- **`_tmp{n}` suffix** — table is dropped within the same file before the function returns; not visible to downstream stages unless `--debug` is set

The current sequence: `_01` → `_02` → `_03a/_03b` → `_04` → `_05`. `inputs.main`'s coverage-clean pass rewrites `_01` in place when violations are detected; it does not introduce a new suffix.

`match` uses its own `name` (`{input}_match`, distinct from `extend`'s
`{input}` so the two tools' tables/files never collide when run against the
same input path and `tmp_dir`) and its own numbering: `{name}_child_01` /
`{name}_parent_01` → `{name}_02_pairs`/`{name}_02_assign`/`{name}_02_unassigned`
→ `{name}_03` (reassembled groups) → `{name}_04` (final coverage-clean). Each
group's own `extend`-pipeline tables (`group_01` … `group_05`, `group_clip`)
live in a private, per-group DuckDB file (`group.duckdb`, one at a time,
reused sequentially) inside `{tmp_dir}/{name}_g{parent_fid}/`, never in
`match`'s own connection — see `docs/matching.md`.

`clean` uses its own `name` (`{input}_clean`, distinct from `extend`/`match`
for the same collision-avoidance reason) and its own numbering: `{name}_01`
→ `{name}_02` (with `_02_tmp1`/`_02_tmp2`/`_02_tmp3` per-kind intermediates,
dropped unless `--debug`) → `{name}_03` (post-`ST_CoverageClean`). No `_04`
whole-table re-clean pass like `extend`/`match` have — `clean` operates on
one table throughout, there's no per-group reassembly seam to close.

### Key Patterns

- **DuckDB spatial extension** handles all geometry operations (`ST_*` functions). One file-backed connection is created per input file in `topo_tools/core/duckdb_utils.py` and returned as a `ProfiledConnection` proxy that logs timing and memory per query when `--debug` is set.
- **DuckDB tables as IPC** — stages read and write named tables on the shared connection; no Parquet between stages.
- **Topology validation** in `_06_outputs.py` (`_check_overlaps`, `_check_gaps`) always runs in outputs, backed by `has_coverage_violations` in `topo_tools/core/extend/_coverage.py`. Both unnest MultiPolygon geometries before checking to ensure correct coverage validation across individual polygon pieces. There is no byte-exactness check — see below.
- **Geometry column names**: `geom` in DuckDB tables, `geometry` in final output.
- **`duckdb_memory()` measurements in isolation underestimate pipeline peaks.** A fresh connection with few tables in the DuckDB file can show 4 GB for a query that peaks at 8 GB in a full pipeline run, because the buffer pool from other large tables (`_01`, `_04`, `_05_tmp1`, etc.) adds several GB of additional pressure. Profile with `--step=X --debug` on a database file that already has all prior-stage tables present.
- **Avoid materializing one global `ST_Union_Agg` of `_01` as a per-row `ST_Difference`/join operand.** At Chile scale the union can hold millions of vertices; using it as an operand against every fid individually made GEOS pay that cost on every row and OOM'd outright (confirmed during development of `_05_merge.py`). Use a bbox-prefiltered join against nearby originals instead (see `_05_merge.py`'s `_05_tmp1`/`_05_tmp2`, which explodes multipolygon fids into parts first — a whole-fid bbox can span mainland-to-remote-island and defeat the prefilter). **`_02_lines.py`'s neighbor-union self-join deliberately does NOT do this** — it joins on whole-fid bboxes. Exploding it into per-part bboxes looks like the same fix but isn't: it helps files with many fids that each have a few widely-scattered parts (e.g. `idn_admin3`) but badly regresses files with one fid made of thousands of tightly-clustered parts (e.g. `chl_admin3` has a single fid with 3,796 parts) by multiplying self-join row count far more than the tighter bboxes save — confirmed empirically (Chile: 3.3GB peak with whole-fid bboxes vs. OOM at 10GB+ with per-part bboxes). See `docs/voronoi-memory.md`.
- **Byte-exact preservation of original polygon vertices is not a goal.** `ST_CoverageClean` may shift any polygon's boundary, including previously-untouched ones. Don't reintroduce per-fid violator scoping, snapshot/restore, or escalation logic to protect vertex-level exactness — that machinery was removed deliberately (see `docs/topology.md`).
- **`core.match` may import from `core.extend`; the reverse is forbidden.** Enforced by the `match-may-use-extend-not-reverse` import-linter contract in `pyproject.toml`. `match` reuses `extend`'s stage functions per-group rather than duplicating Voronoi gap-filling logic; `extend` must stay usable standalone with zero knowledge of `match`.
- **`match`'s per-group work runs in an isolated subprocess, not `match()`'s own connection/process.** GEOS's native heap isn't fully released between files even after closing the DuckDB connection (the same finding that makes `extend()` process one file per OS process) — a many-parent-group `match()` run would hit the same failure mode in-process, just with groups substituting for files. See `docs/matching.md`.
- **`core.clean` may import from `core.extend`; the reverse is forbidden.** Enforced by the `clean-may-use-extend-not-reverse` import-linter contract. `clean` reuses `extend`'s `read_and_reproject()` (inputs, without the auto-clean pre-check) and `coverage_clean()` (fix stage); `extend` must stay usable standalone with zero knowledge of `clean`.
- **`ST_CoverageClean`'s `gap_maximum_width` has no GEOS-native auto-fill default.** Verified against upstream source (duckdb-spatial's `geos_module.cpp`, GEOS's `CoverageCleaner.h`/`.cpp`): the C++ class member is hardcoded to `0.0`, and a negative/omitted value is a no-op that leaves it there — unlike `snapping_distance`, which does have a real computed auto-default (`extent_diameter / 1e8`). `clean`'s `--gap-width all` mode computes an explicit width from the widest *detected* gap rather than relying on any GEOS-side "auto-fill." See `docs/cleaning.md`.
- **`ST_Distance(GEOMETRY, GEOMETRY)` is unreliable for two disjoint polygons at small separations** — confirmed it returns `0.0` for two clearly-separated polygons (~3cm apart) on the installed DuckDB version, while the equivalent POINT/LINESTRING pair correctly returns the true distance. Use `ST_XMin`/`ST_XMax`/`ST_YMin`/`ST_YMax` extent comparisons or `ST_MaximumInscribedCircle` instead when checking polygon disjointness/gap width.

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
- `docs/matching.md` — match's largest-overlap assignment algorithm, per-group subprocess isolation rationale, the `fids=None` whole-table-clean constraint, and the check_gaps/parent-layer-gaps caveat
- `docs/cleaning.md` — clean's gap/overlap/sliver detection approach, why slivers are detect-only, verified `ST_CoverageClean` parameter semantics (`gap_maximum_width` has no GEOS-native auto-fill default, unlike `snapping_distance`), and the issues-file schema
- `docs/performance.md` — thread-scaling benchmarks, pipeline phase profiles, `get_connection` settings, RTREE experiment
- `docs/voronoi-memory.md` — Voronoi collinearity degeneracy fix (segment cap, dynamic resampling distance), `--memory-gb`-derived point budget fitted inside a real memory-limited Docker container, and two documented (not gated) memory ceilings in `inputs.py`/`lines.py` that genuinely exceed 4GB for large files (`phl_admin3`, `idn_admin3`)
- `docs/publishing.md` — PyPI release process (GitHub Release → required-reviewer approval → trusted-publisher OIDC), and the TestPyPI rehearsal loop for testing packaging changes
