# Change Reference

`change` compares two versions of a polygon layer (old and new) and
classifies every unit as `unchanged` / `renamed` / `modified` / `relocated` /
`split` / `merge` / `complex` / `created` / `removed`, using spatial overlap
and, optionally, code/name identity linking. Ported from the sister JS app's
interactive tool (`topo-tools-js/src/lib/tools/polygon-changelog/`, still
internally named `polygon-changelog`/`cw_*` from its original name "Boundary
Cross-walk"); this doc covers what changed in the port and the
classification algorithm's non-obvious pieces.

## Usage

```sh
topo-tools change old.geojson new.geojson
```

```python
from topo_tools import change

change("admin3_v02.geojson", "admin3_v03.geojson")
```

`OUTPUT_FILE` (positional, optional; the tabular changelog, CSV or
GeoParquet) defaults to a name combining both stems with a `_changelog`
suffix. A spatial overlay layer colored by `relationship_class` is always
written alongside it.

| Option | Description |
| --- | --- |
| `--overlay-file` | Spatial overlay layer path. Defaults to `OUTPUT_FILE` with an `_overlay` suffix. |
| `--tau-match` | Minimum overlap coverage for two units to be spatially linked (default `0.8`). |
| `--tau-same` | Minimum IoU for a 1:1 linked pair to be unchanged/renamed rather than modified (default `0.98`). |
| `--link-by-code` | Also link units sharing a unique code value across versions. |
| `--link-by-name` | Also link units sharing a unique name value across versions. |
| `--link-mode` | How code/name identity matches combine (`either`/`both`, default `either`; only matters if both link flags are set). |
| `--code-column-a` / `--code-column-b` | Old/new-side code column; auto-detected if omitted. |
| `--name-column-a` / `--name-column-b` | Old/new-side name column; auto-detected if omitted. |
| `--overwrite` | Overwrite an existing output file. |
| `--threads` | DuckDB thread count. |
| `--debug` | Keep intermediate tables, export to Parquet, log timing/memory per query. |
| `--tmp-dir` | Intermediate DuckDB + Parquet location. |
| `--step` | Run only one named stage: `inputs`, `overlap`, `classify`, `outputs`. |

```sh
# Also link units sharing a unique pcode across versions
topo-tools change old.gpkg new.gpkg --link-by-code

# Loosen the "related" threshold for heavily redrawn boundaries
topo-tools change old.parquet new.parquet --tau-match 0.6
```

Run `topo-tools change --help` for the full, always-current option list.

## Pipeline

1. **`_01_inputs`** — loads and coverage-cleans both layers by delegating
   twice to `extend`'s own loader (`{name}_a_01` = old, `{name}_b_01` = new).
   Unlike `clean`, `change` isn't trying to detect defects in the raw
   input — it's comparing two whole layers — so pre-cleaning each side
   reduces the risk of native GEOS choking on invalid geometry during
   `ST_Intersection`, with no downside (same reasoning as `match`'s inputs
   stage, which also pre-cleans both its layers).
2. **`_02_overlap`** — computes `shared_area`/`coverage_a`/`coverage_b`/`iou`
   for every touching `(a_fid, b_fid)` pair.
3. **`_03_classify`** — identity + spatial union-find clustering and
   cardinality-based classification, run in Python; assembles the final
   changelog table.
4. **`_04_outputs`** — builds the spatial overlay render layer and exports
   both artifacts. No topology hard-gate here (unlike `extend`/`match`/
   `clean`): `change` is a read-only comparison, not a fix, so there's
   nothing to validate against.

## Why the WASM point-sampling fallback is dropped

JS's `src/lib/db/overlap.ts` tries exact `ST_Intersection` first, and falls
back to a 32×32 point-sampling estimate on failure — working around a
documented WASM-only GEOS OverlayNG bug ("found non-noded intersection") on
near-coincident, independently-digitized boundaries. JS's own commit history
(`0672282`, replacing an earlier noded-overlay retry) found this fallback
"fails identically even on the native CLI and adds no value" — i.e. the bug
is WASM-specific. Since this repo runs native DuckDB, the port always uses
exact `ST_Intersection`; there is no sampling fallback, no
`ComparisonMethod` enum, and no capping `tau_same` at 0.99 for a sampling
run (JS's UI-only accommodation for the fallback's lower precision).

## Overlap computation

`_02_overlap.py` mirrors `core/match/_02_assign.py`'s proven pattern: both
layers are exploded into parts (`UNNEST(ST_Dump(geom))`) before the join, so
a multi-part fid (a country with offshore islands) doesn't get one bbox
spanning everything and defeat the prefilter. The join uses scalar
`ST_XMin`/`ST_XMax`/`ST_YMin`/`ST_YMax` predicates, not `ST_Within`/
`ST_Intersects` alone in the `JOIN` condition — that triggers DuckDB's
`SPATIAL_JOIN` operator and its ~1x-RAM virtual reservation (see
`docs/topology.md`). Unlike `_02_assign.py` (which keeps only the top-1
parent per child), every pair with `shared_area > 0` is kept: classification
needs the full pair graph, not just the best match per fid.

Intersection crumbs below `INTERSECTION_SLIVER_DEG2` (`1e-12` deg², ~1cm²,
ported as-is from JS) are dropped by their raw, untransformed degree² area —
a cheap pre-filter before the equal-area transform, which is only ever
applied to surviving intersection geometry (not the whole layer, to bound
the cost). Areas and ratios use `EPSG:8857` (Equal Earth), matching `match`'s
own reasoning for avoiding raw `EPSG:4326` degree-area bias toward
higher-latitude units. `EQUAL_AREA_CRS` is duplicated as a literal in
`change`'s own `_constants.py` rather than imported from `core.match` —
`change` stays decoupled from `match`/`clean` the same way they're
decoupled from each other (no import-linter contract needed for one string
constant).

## Classification: identity + spatial union-find

`_03_classify.py` ports `classify.ts` line-for-line, but runs the union-find
and cardinality classification in Python rather than SQL. This is safe under
this repo's memory model: the algorithm scales with **feature count**, not
vertex count — a 500K-polygon admin layer is trivial to hold as Python
dicts/sets, unlike the vertex-scaled Voronoi/coverage-clean work `extend`/
`clean` do. Pair rows are fetched once via `conn.execute(...).fetchall()`,
classified entirely in memory, and written back via `conn.executemany()` —
no batching needed the way JS's raw-SQL-string `INSERT ... VALUES` batching
was (`_unionfind.py` mirrors JS's path-compressed, union-by-rank
`unionFind.ts` directly).

**Two-phase matching:**

1. **Identity** (only when `--link-by-code`/`--link-by-name` is set): a pair
   is a candidate identity match if its code and/or name values are equal
   **and unique** on each side (duplicate values like a shared "No_Pcode"
   placeholder are excluded — matching on them would union every polygon
   sharing that value into one cluster). NULL-safe: a NULL code/name on
   either side never counts as a match.
2. **Spatial**: any pair with `max(coverage_a, coverage_b) >= tau_match` is
   unioned, unless already claimed by phase 1.

**The identity claim guard** (ported from JS commit `f844472`) is the most
load-bearing piece of this stage: an identity-matched pair is only
pre-unioned ahead of spatial matching if *every other spatial
tau_match-passing neighbor* of both fids is *also* identity-covered.
Without this guard, a genuine split — old unit A splits into new B1
(inheriting A's code) and new B2 (a new code) — would incorrectly collapse
into a false 1:1 identity match on A↔B1, leaving B2 stranded as a spurious
`created` unit instead of correctly grouping A, B1, and B2 into one `split`
cluster. The guard defers to phase 2's spatial clustering whenever any
spatial neighbor lacks its own identity match, letting the whole cluster
resolve by cardinality instead. Verified with a synthetic fixture
(`tests/test_change.py`'s `GD1`/`GD2` region): a real split where only
one child inherits the parent's code stays classified `split` under
`--link-by-code`, not a false `unchanged`/`renamed` pair plus an orphaned
`created`.

**Cardinality classification** (`na`/`nb` = member count per side of a
connected component):

| na | nb | condition | class |
|----|----|-----------|-------|
| 1  | 0  | — | `removed` |
| 0  | 1  | — | `created` |
| 1  | 1  | identity-only, no spatial `tau_match` pass | `relocated` |
| 1  | 1  | spatial pass, `iou >= tau_same`, code/name unchanged | `unchanged` |
| 1  | 1  | spatial pass, `iou >= tau_same`, code/name differ | `renamed` |
| 1  | 1  | spatial pass, `iou < tau_same` | `modified` |
| 1  | >1 | — | `split` |
| >1 | 1  | — | `merge` |
| >1 | >1 | — | `complex` |

`renamed` only fires when linking is enabled — in pure geometry mode,
code/name are never consulted for classification (per JS commit `7244e8a`'s
"geometry-first mode never consults code/name for renamed"), only for
display in the output table.

## Output artifacts

JS's `export.ts` registers two sources (`crosswalk_changelog` tabular,
`crosswalk_overlay` spatial) but only wires the tabular one to a download
button — the spatial overlay exists internally but was never reachable from
the UI. Headless has no such constraint, so `change()` **always** writes
both:

- **Tabular changelog** (`output_path`, `.csv` default or `.parquet`, no
  geometry column) — one row per matched pair, plus one row per unmatched
  singleton (pure `created`/`removed`, or a `split`/`merge` remnant with
  nothing on the other side). Columns: `code_a, name_a, code_b, name_b,
  relationship_class, match_method, a_in_b (coverage_a, 3dp), b_in_a
  (coverage_b, 3dp), similarity (iou, 3dp), threshold_match, threshold_same,
  link_by_code, link_by_name, link_mode`. The last five columns echo the
  run's own parameters into every row (ported from JS commit `06c073a`, "so
  identity-mode runs are self-documenting") — a reproducibility/audit-trail
  feature worth preserving for a batch tool where the invocation's flags
  aren't otherwise recorded alongside the output. `code_a`/`code_b`/
  `name_a`/`name_b` are `NULL` unless the corresponding `--code-column-*`/
  `--name-column-*` was resolved (explicitly or via auto-detection under a
  `--link-by-*` flag) — a pure spatial run with no linking and no explicit
  column has no identity columns to report.
- **Spatial overlay layer** (`overlay_path`, defaults to `old_path`'s own
  format) — every new-version unit tagged with its `relationship_class`,
  plus every old-version unit classed `removed` (gone in the new version, so
  no new polygon stands in for it). Together these tile the comparison area
  exactly once, colored by what happened (ported from JS's
  `render.ts:stageRender`). Single geometry type (Polygon/MultiPolygon), so
  any of `extend`'s four formats is valid, unlike `clean`'s mixed-type
  issues file.

## Column auto-detection

`core/change/_columns.py` ports JS's `src/lib/db/columns.ts` regex
patterns verbatim — same priority order, same first-match-wins logic. Only
consulted when a `--link-by-code`/`--link-by-name` flag is set and its
column isn't explicitly named; an explicit `--code-column-*`/
`--name-column-*` always overrides. If linking is requested and no column
can be found or was given, `change()` raises `ValueError` rather than
silently falling back to geometry-only — an explicit ask deserves an
explicit failure, not silent divergence from what the user asked for.

## Thresholds

- **`--tau-match`** (default `0.8`, JS's current default per commit
  `420f2ad`) — minimum `max(coverage_a, coverage_b)` for two units to be
  spatially linked (a union-find edge). Note the `max`, not `coverage_a`
  alone: a small split fragment that's 100% contained in its parent
  (`coverage_b = 1.0`) still clears the bar even though it might be only a
  third of the parent's own area (`coverage_a = 0.33`) — this is what lets
  split/merge fragments connect regardless of their share of the original
  unit.
- **`--tau-same`** (default `0.98`) — minimum IoU for a 1:1 spatially-linked
  pair to be `unchanged`/`renamed` rather than `modified`.

Both are plain floats in `[0, 1]`, with no `auto`/`all` string modes the way
`clean`'s `--gap-width` has — these thresholds have no GEOS-native
auto-default to defer to, they're this tool's own tunables.

## No `--memory-gb`

Like `clean`, there's no Voronoi stage to size a resampling budget for.
