# Matching Reference

`match` fits a **child** polygon layer into a coarser **parent**/clip layer
(e.g. admin4 into admin0), reusing `extend`'s Voronoi gap-filling pipeline
under the hood. This doc covers the parts that are specific to `match`: the
overlap/assignment algorithm, the per-group subprocess design, and the
constraints it inherits from `extend`. See `docs/topology.md` for the
coverage-clean/`SPATIAL_JOIN` background both tools share.

## Usage

```sh
topo-tools match children.geojson parents.geojson
```

```python
from topo_tools import match

match("admin4.geojson", "admin0.geojson", "admin4_matched.geojson", memory_gb=4)
```

`OUTPUT_FILE` (positional, optional) defaults to `INPUT_FILE` with a
`_matched` suffix.

| Option | Description |
| --- | --- |
| `--memory-gb` | Available memory in GB; sizes point density automatically (default `4`). |
| `--overwrite` | Overwrite an existing output file. |
| `--threads` | DuckDB thread count. |
| `--debug` | Keep intermediate tables, export to Parquet, log timing/memory per query. |
| `--tmp-dir` | Intermediate DuckDB + Parquet location. |
| `--step` | Run only one named stage: `inputs`, `assign`, `groups`, `merge`, `outputs`. |

```sh
# Fit an admin4 layer into a single country boundary
topo-tools match adm4.geojson adm0.geojson

# Fit admin3 into admin2 groups, each cleaned against its own parent
topo-tools match adm3.gpkg adm2.gpkg adm3_matched.gpkg --memory-gb 2
```

Each parent's group of children runs in its own isolated subprocess, so a run
with many parents (e.g. matching a nationwide admin4 layer against dozens of
admin2 units) scales without one large group's memory use affecting another's.

Run `topo-tools match --help` for the full, always-current option list.

## Pipeline

1. **`_01_inputs`** — loads and coverage-cleans both layers by delegating
   twice to `extend`'s own loader (`{name}_child_01`, `{name}_parent_01`).
2. **`_02_assign`** — assigns each child to the parent it shares the largest
   area with (plurality, not majority); drops and logs children with zero
   overlap with any parent.
3. **`_03_groups`** — groups children by their assigned parent (always, even
   a group of exactly one parent), runs `extend`'s pipeline within each group
   in an isolated subprocess, clips each group's result to its own parent,
   reassembles the survivors.
4. **`_04_merge`** — a single whole-table `ST_CoverageClean` pass over the
   reassembled output to close cross-group seams.
5. **`_05_outputs`** — validates topology and exports.

## Largest-overlap assignment

Ported from the sister JS app's `match` tool (`overlap.ts`/`assign.ts`), with
the WASM-only workarounds (point-sampling overlap fallback, precision-retry
clip) dropped — native DuckDB/GEOS doesn't need them.

Both layers are exploded into parts (`UNNEST(ST_Dump(geom))`) before
computing bbox candidates, exactly like `_05_merge.py`'s `_05_tmp1`: a
multi-part parent (a country with offshore islands) would otherwise get one
bbox spanning everything and defeat the prefilter. The join uses scalar
`ST_XMin`/`ST_XMax`/`ST_YMin`/`ST_YMax` predicates, not `ST_Within`/
`ST_Intersects` in the `JOIN` condition — that triggers DuckDB's
`SPATIAL_JOIN` operator and its ~1x-RAM virtual reservation (see
`docs/topology.md`).

Shared area per `(child, parent)` fid pair is summed across every part-pair
(a multi-part child can overlap a multi-part parent in more than one place),
ranked in an equal-area CRS (`EPSG:8857`, Equal Earth) rather than raw
EPSG:4326 degree-area — only the intersection geometry is transformed, not
the whole layer, to bound the cost. Plain degree-area would bias plurality
assignment toward higher-latitude parents; verified DuckDB resolves the
`EPSG:4326` → `EPSG:8857` transform offline (no network needed once the
`spatial` extension itself is cached).

```sql
ROW_NUMBER() OVER (PARTITION BY child_fid ORDER BY shared_area DESC, parent_fid ASC)
```

picks the plurality parent per child; ties break on the lowest parent fid.
Children with zero overlap with any parent are dropped with a logged
warning (`match: dropping N unmatched child fid(s) with no parent overlap:
[...]`), not an error — a real dataset (e.g. a national admin4 layer matched
against a coarser admin0/admin2 clip layer with gaps of its own) can
legitimately have children outside every parent's territory.

## Per-group subprocess isolation

Each group's `extend` pipeline (`_02_lines` → `attempt` → `_05_merge`) runs in
its own fresh `multiprocessing` (`spawn` context) subprocess, not the parent
`match()` call's shared connection. This is a deliberate design choice, not
an afterthought: CLAUDE.md documents a real, previously-confirmed finding
that GEOS's native heap isn't fully released between files even after
closing the DuckDB connection, which is exactly why `extend()` processes one
file per OS process today. A many-parent-group `match()` run (e.g.
admin4-into-admin2 for a country with dozens or hundreds of admin2 units)
would hit the identical failure mode in-process, just with groups
substituting for files, if groups shared one process. Building the same
per-file-per-process isolation guarantee down to group granularity avoids
that outright rather than hoping in-process cleanup is sufficient.

Data crosses the process boundary as small Parquet files (`child.parquet`,
`parent.parquet` in, `output.parquet` out), never a shared connection — a
DuckDB file is single-writer, and the group's own DuckDB file
(`group.duckdb`) lives entirely inside that group's private temp directory,
discarded when the group finishes (unless `--debug`). Verified empirically
that a `GEOMETRY` column round-trips correctly through
`COPY ... TO (FORMAT PARQUET)` + `read_parquet(...)` in the installed DuckDB
version — no `GEOPARQUET_VERSION` option is needed for this internal,
DuckDB-to-DuckDB round trip.

If a group's subprocess fails (OOM, or exhausts `attempt.py`'s 10 retries),
`match()` logs an error naming that parent's fid and drops its children from
the output, then continues with the remaining groups — consistent with the
existing drop-unmatched-children-with-a-warning behavior, rather than
aborting an entire multi-country/multi-region run over one bad group.
`match()` raises only if **no** group produced any output at all.

A freshly-spawned process has no logging configuration of its own
(`basicConfig` only ever runs in `cli/main.py`, in the parent process) — the
worker puts a success/error signal on a `multiprocessing.Queue` instead of
relying solely on its own log output, and only configures logging locally
(mirroring `cli/main.py`'s own call, teed to a per-group log file) when
`--debug` is set, so `ProfiledConnection`'s per-query timing/RSS output isn't
silently dropped during a debug run.

**Real-world smoke test**: `bdi_admin4.gpkg` (3,067 features) matched against
`bdi_admin2.parquet` (119 parents) completed successfully end-to-end — 119
subprocess spawns, zero dropped children, zero failed groups, valid output
coverage (see verification steps in the project's implementation history).

**Colombia-scale profiling** (portolan `col/latest/adm3` → `col/latest/adm2`,
`--memory-gb 4 --debug`, Apple Silicon/10 logical cores): 31,880 children
against 1,122 parents, 1,120 of them with at least one assigned child (the
other 2 parents had zero overlapping children — not a failure, no adm3 unit
fell inside them). All 1,120 spawned subprocesses succeeded — zero dropped
children, zero failed groups. Wall time 35m44s, peak RSS 5.26 GB (during the
final whole-table `_04_merge` coverage-clean pass, exceeding the 4 GB
`--memory-gb` soft target — same "document, don't gate" situation as
`clean`'s Philippines run, see `docs/clean.md`). Stage breakdown:

| Stage    | Wall time | Share |
| -------- | --------- | ----- |
| inputs   | 1m06s     | 3%    |
| assign   | 57s       | 3%    |
| groups   | 30m45s    | 86%   |
| merge    | 53s       | 2%    |
| outputs  | 2m02s     | 6%    |

`groups` dominates as expected (1,120 sequential subprocess spawns, ~1.65s/
group average including Python/DuckDB startup, the per-group `extend`
pipeline, and teardown) but shows no cliff or superlinear blowup relative to
Burundi's 119-group run — per-group spawn overhead is not a bottleneck at
this scale.

## `fids=None`: whole-table coverage-clean only

`_04_merge.py` calls the shared `coverage_clean()` helper with `fids=None`
(whole-table), matching `extend`'s own two callers (`_01_inputs.py`,
`_05_merge.py`). **Do not scope this to a subset of fids for performance**,
even though `coverage_clean()` technically accepts a `fids` list — per-fid
violator scoping was deliberately removed from `extend`'s own merge stage
once already because it reintroduced seam-gap bugs (see `docs/topology.md`).
By construction, every point of the reassembled extent belongs to exactly
one surviving child fid, so anything `ST_CoverageClean` finds to close here
is seam noise (float-precision mismatches at group-to-group boundaries and
each group's own clip line), not a real feature to protect.

## `check_gaps` and parent-layer gaps

`_05_outputs.py` reuses `extend`'s `check_overlaps`/`check_gaps` (hoisted
into `topo_tools/core/extend/_coverage.py` as public functions so `match` can
import them without a private cross-package import) unmodified, on the
final `{name}_04` table. This cannot distinguish a gap `match`'s own clip
step introduced from a gap the parent/clip layer already had between two
different parents' territories (e.g. a world ADM0 layer with disputed or
unclaimed areas). This is intentional: a gap here is a real signal that the
clip layer itself needs `extend` treatment first, not something `match`
should silently paper over.

## Debug tables

`--step=groups --debug` exports everything currently in the connection
(`{name}_child_01`, `{name}_parent_01`, `{name}_02_pairs`, `{name}_02_assign`,
`{name}_02_unassigned`, `{name}_03`), the same as a full run — group ids
aren't known ahead of time, so there's no static table list to filter to for
that step. Per-group internal detail (the group's own `group.duckdb`,
`group.log`, `child.parquet`, `parent.parquet`, `output.parquet`) is
preserved under `{tmp_dir}/{name}_g{parent_fid}/` when `--debug` is set,
inspectable independently of the main connection's exports.
