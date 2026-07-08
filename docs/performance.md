# Performance Notes

Benchmarks and analysis for memory-constrained deployment (DuckDB-WASM, Docker).

Machine: Apple Silicon, macOS, 10 logical cores.

---

## Container / platform notes

- No wheel is published for Alpine/musl (`python:*-alpine` images) or for glibc
  <2.26 (e.g. RHEL/CentOS 7, Amazon Linux 2) — these can't get a prebuilt
  `duckdb` wheel from PyPI. Use a glibc-based image (e.g. `python:3.x-slim`)
  instead.
- Every CLI flag also has an environment variable equivalent (`INPUT_FILE`,
  `MEMORY_GB`, `DEBUG`, etc. — see `topo_tools/cli/main.py`), useful when
  flags are awkward to pass in a container entrypoint.
- Air-gapped or network-restricted environments need the DuckDB `spatial`
  extension pre-installed rather than downloaded on first run (see
  [DuckDB's extension docs](https://duckdb.org/docs/extensions/overview)).

## Why one file per process

`extend()`/`topo-tools extend` process exactly one file per call by design.
Looping over many files *within a single process* has caused unbounded memory
growth in the past: GEOS's native heap isn't fully released between files, even
with the DuckDB connection closed. Call it once per file from separate OS
processes instead.

## Memory profiling methodology

**RSS peak is the primary metric** for Docker/WASM sizing. `duckdb_memory()` is unreliable
in both directions:

- **Undercounts GEOS working memory**: `ST_VoronoiDiagram`, `ST_Node`, `ST_Polygonize`
  allocate through GEOS's own heap — completely invisible to DuckDB's allocator tracking.
  For Chile `_04_tmp1` at 1 thread, this gap is ~6.9 GB (0.9 GB duckdb vs 7.8 GB RSS).
- **Overcounts when spilling**: the DuckDB buffer pool counts pages it has spilled to the
  `.duckdb` file as still "allocated". For Chile `_05`, this inflated the duckdb peak by
  ~2.5 GB (8.1 GB duckdb vs 5.5 GB RSS).

The `--debug` flag logs `rss peak` (from `psutil.Process().memory_info().rss`, sampled
every 50 ms) alongside `duckdb delta/total` for table-accumulation context.


---

## Pipeline phase profiles

RSS peak per phase for Chile admin3 at 1 thread:

| Phase       | Module       | RSS Peak     | Wall time | Main bottleneck                                           |
| ----------- | ------------ | ------------ | --------- | --------------------------------------------------------- |
| Input       | `_01_inputs.py`  | 680 MB       | ~1s       | I/O                                                       |
| Lines       | `_02_lines.py`   | 2,708 MB     | ~28s      | Self-join bbox neighbor union + GEOS line extraction      |
| Points      | `_03_points.py`  | 2,282 MB     | ~7s       | Interpolation + endpoint union                            |
| **Voronoi** | `_04_voronoi.py` | **7,249 MB** | ~275s     | `ST_VoronoiDiagram` + fid join + `ST_Union_Agg`           |
| Merge       | `_05_merge.py`   | ~2,900 MB    | ~44s      | bbox-prefiltered per-fid `ST_Difference` (`_05_tmp2`) + whole-table `ST_CoverageClean` (`_05`) |
| Outputs     | `_06_outputs.py` | 6,005 MB     | ~3s       | `check_gaps` `ST_Union_Agg` + COPY                        |

**Voronoi** (`_04_tmp1`) is the pipeline peak at ~7.2 GB. The stage has three steps:
`_04_tmp1` collects all points and calls `ST_VoronoiDiagram` (GEOS heap, invisible to
`duckdb_memory()`; most of the peak is hidden at 1 thread); `_04_tmp2` assigns a source
`fid` to each cell via `ST_Intersects` (thread-sensitive: 100s at 1t); `_04` unions
cells by `fid` via `ST_Union_Agg` (single-threaded GEOS, ~141s flat across all thread
counts). The retry/doubling-distance mechanism in `attempt.py` is the safety valve: it
backs off from 10M points until the operation fits in available memory.

**Lines** (`_02b`) used to be the first ~6 GB breach in the pipeline. After the
bbox-self-join rewrite (see below), the stage peaks at ~2.7 GB at 1 thread —
Voronoi is now the only stage that crosses 6 GB.

**Outputs topology checks**: `check_overlaps` is a self-join that could degrade to O(n²)
pairs without a spatial index, but DuckDB's `SPATIAL_JOIN` rewrite handles non-overlapping
polygon sets cheaply via bounding-box rejection. `check_gaps` runs `ST_Union_Agg` on all
final polygons — the most expensive single query in the outputs phase.

---

## Portolan-scale profiling (post-restructure)

The 147-file batch run recorded in `docs/voronoi-memory.md` predates the
`app/` → `topo_tools` package restructure (commit `76f4426`); `clean` and
`change` both got fresh post-restructure real-data tables (see
`docs/clean.md`, `docs/change.md`) but `extend` itself hadn't been re-run
against real portolan data under the current code until now.

`phl_admin3` (portolan `phl/latest/adm3`, 1,642 fids, 13.85M vertices —
the same file `docs/voronoi-memory.md` documents as needing ~5.9GB in
`_01_inputs.py`'s coverage-clean fallback path), `--memory-gb 4 --debug`,
Apple Silicon/10 logical cores:

| Stage   | Wall time | Notes                                                    |
| ------- | --------- | --------------------------------------------------------- |
| inputs  | 12s       | no invalid edges detected — fallback `ST_CoverageClean` did **not** trigger |
| lines   | 39s       |                                                             |
| attempt | 2m13s     | 13.07M raw segments needed ~11.1GB to decompose/remerge alone, exceeding the ~4GB budget before resampling — proceeded anyway with `DEFAULT_DISTANCE` per the soft-target policy, succeeded on the first attempt (no retry) |
| merge   | 1m24s     |                                                             |
| outputs | 1m47s     |                                                             |
| **Total** | **6m15s** | peak RSS **4.55 GB**                                     |

Output: 1,642 fids preserved, 905,538 vertices (down from 13.85M — expected,
`extend` resamples/simplifies via the Voronoi step). Topology validation
passed (`check_overlaps`/`check_gaps`), no correctness issues.

**This run did not exercise the documented ~5.9GB ceiling** — that figure is
specifically `_01_inputs.py`'s whole-table `ST_CoverageClean` fallback,
which only fires when `ST_CoverageInvalidEdges_Agg` finds invalid edges;
`phl_admin3`'s source data has none, so `inputs` took the no-op fast path.
The 4.55GB peak measured here is the normal (no-fallback) pipeline cost for
this file post-restructure — lower than the ~5.9GB ceiling, as expected
since that ceiling is for a different, more expensive code path within the
same stage. If the invalid-edge fallback ever needs re-validating
post-restructure, it needs a file that actually trips
`ST_CoverageInvalidEdges_Agg`, not a clean one.

---

## Thread-scaling benchmarks (Chile admin3)

| threads    | pipeline peak RSS | `_04_tmp2` time | `_04` time | `_02b` time | `_05` time | total time |
| ---------- | ----------------- | --------------- | ---------- | ----------- | ---------- | ---------- |
| 1          | **7,249 MB**      | 99.3s           | 139.8s     | 7.4s        | 2.1s       | ~318s      |
| unset (10) | 6,776 MB          | 56.3s           | 140.3s     | 7.5s        | 1.9s       | ~271s      |

Pipeline peak is `_04_tmp1` (Voronoi point collection + diagram) at all thread counts.

Key thread-sensitivity breakdown:
- `_04_tmp2` (fid assignment via `ST_Intersects`): 100s → 56s, 1.8× faster with more threads
- `_04` (`ST_Union_Agg` by fid, single-threaded GEOS): ~141s → ~140s, flat — the hard ceiling
- `_02b` (line extraction, bbox-self-join): 7.4s → 7.5s, no gain — `PIECEWISE_MERGE_JOIN` is single-threaded internally
- `_05` (cell-point ST_Within `_01` then `_04` fallback): 1.8s → 1.9s, negligible

For memory-constrained deployments: `--threads=1` gives a similar peak (~7.2 GB) to
default threads. Both are above a 4 GB WASM/Docker target — reducing below that requires
pipeline changes (chunking or a coarser `--memory-gb`-derived resampling distance, see
`docs/voronoi-memory.md`).

---

## `get_connection` settings

| Setting                            | Effect                                                                                                                                                                                            |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOAD spatial`                     | One-time extension load. No ongoing effect.                                                                                                                                                       |
| `enable_progress_bar = false`      | No memory or performance effect. Suppresses terminal noise.                                                                                                                                       |
| `geometry_always_xy = true`        | No memory or performance effect. Correctness: forces (lon, lat) coordinate order regardless of CRS definition. Required for correct EPSG:4326 output.                                             |
| `preserve_insertion_order = false` | **Free win.** Removes sequence-tracking overhead from every intermediate buffer and eliminates the reorder pass after parallel aggregations. Workers emit chunks immediately rather than queuing. |
| `threads = N`                      | DuckDB thread count per connection.                                                                                                                                                               |

**`memory_limit` is unset** (defaults to 80% of system RAM). On a dev machine this is
fine; in a Docker container DuckDB doesn't know it's constrained and will allocate freely
until the OOM killer fires. For Docker, set `memory_limit` explicitly (e.g. `'1500MB'` in
a 2 GB container) so DuckDB can spill to disk rather than crash.

---

## Lines stage bbox-self-join

The lines stage previously expressed the per-polygon "neighbor boundary union" as a
`LATERAL` subquery containing `ST_Intersects(a.geom, b.geom)`, which DuckDB rewrites to
the `SPATIAL_JOIN` operator. `SPATIAL_JOIN` pre-allocates ~1× RAM as a virtual spill
reservation — the same reservation behavior documented in `topology.md` — so even on
small data the lines stage held a multi-GB working set.

The current form materializes neighbor unions via a self-join with **scalar bbox
predicates only**, then keys `_02` off the resulting `_02_tmp2` table:

```sql
CREATE TABLE _02_tmp2 AS
SELECT a.fid AS afid, ST_Union_Agg(b.geom) AS neighbor_union
FROM _02_tmp1 AS a
JOIN _02_tmp1 AS b
  ON a.fid != b.fid
 AND ST_XMax(b.geom) >= ST_XMin(a.geom)
 AND ST_XMin(b.geom) <= ST_XMax(a.geom)
 AND ST_YMax(b.geom) >= ST_YMin(a.geom)
 AND ST_YMin(b.geom) <= ST_YMax(a.geom)
GROUP BY a.fid
```

DuckDB plans this as `PIECEWISE_MERGE_JOIN` + `HASH_GROUP_BY` — no `SPATIAL_JOIN`.
Bbox-only is correct because a non-touching neighbor adds nothing to subsequent
`ST_Difference` / `ST_Intersection` against `a`'s boundary, so the loose prefilter is
conservative-but-equivalent. Empirically the bbox prefilter is as selective as
`ST_Intersects` on tessellated admin layers (avg 6.4 neighbors/poly on both Burundi and
Chile, max 17).

Result on Chile at 1 thread: lines stage peak drops from 6,081 MB → 2,708 MB (−55%) and
wall time drops from ~53s → ~28s (−47%). End-to-end `_05` outputs are byte-equivalent
(`ST_Equals` per fid, 0% sym-diff).

The same bbox-self-join pattern applies in `_05_merge.py`'s `_05_tmp1`/`_05_tmp2` (see
below) — both explode multipolygon fids into parts first, since a whole-fid bbox can
span mainland to a remote island (Chile's Easter Island case) and defeat the prefilter
otherwise.

---

## Merge stage memory profile (Chile)

RSS peaks for the merge stage queries at 1 thread, measured with `--step=merge --debug`
on a database file that already has all prior-stage tables resident (`_01`, `_02`, `_03a`,
`_03b`, `_04`), per the profiling methodology above.

| Query      | Time   | RSS peak | Notes                                                              |
| ---------- | ------ | -------- | ------------------------------------------------------------------ |
| `_05_tmp1` | 0.2s   | 495 MB   | Per-part `_01` with bbox columns (parts, not whole fids)           |
| `_05_tmp2` | 22.8s  | 2,900 MB | Per-fid bbox-prefiltered neighbor union + `ST_Difference` against `_04`; pipeline peak for this stage |
| `_05_tmp3` | 19.7s  | 2,068 MB | Dissolve to one row per fid, reattach original attributes         |
| `_05`      | 1.6s   | 1,815 MB | Whole-table `ST_CoverageClean` via the shared `coverage_clean` helper |

Total ≈44s, peak ≈2.9 GB — down 51% from the previous `ST_Node`/`ST_Polygonize` design's
5,953 MB peak, though wall time is roughly 10× slower (~4s → ~44s). The `_05_tmp2` bbox
self-join (Voronoi cell vs. original-polygon-part bboxes) is the new bottleneck; the
`ST_CoverageClean` call itself is cheap and was not the risk this design predicted —
memory pressure came from the union/difference step, not the coverage-clean step.

**A single global `ST_Union_Agg(_01)` used as the `ST_Difference` operand OOMs outright**
at Chile scale (`failed to allocate data of size 16.0 MiB (12.7 GiB/12.7 GiB used)`,
observed during development) — the union itself is cheap to compute, but using a
multi-million-vertex geometry as a per-row `ST_Difference` argument against thousands of
fids is a different, much more expensive access pattern. The bbox-prefiltered per-part
neighbor union above is the fix; see `docs/topology.md`, "Why not a single global union."

---

## RTREE index experiment

Tested adding explicit RTREE indexes at every candidate spatial join site across the full
pipeline (Chile admin3, default threads). Three configurations:

- **none** — no RTREEs anywhere
- **merge** — RTREE only on `_05_tmp4` (former default)
- **all** — RTREEs on `_01`, `_02_tmp1`, `_04_tmp1`, and `_05_tmp4`

**Wall time (seconds) at key queries:**

| Query | none | merge | all | join type |
|---|---|---|---|---|
| `_02a` | 11.8 | 11.1 | 11.2 | LATERAL + ST_Intersects on `_02_tmp1` |
| `_02b` | 18.0 | 17.6 | 17.1 | LATERAL + ST_Intersects on `_02_tmp1` |
| `_04_tmp1` index | — | — | **0.9** | index build cost |
| `_04_tmp2` | **50.3** | **55.9** | **57.3** | ST_Intersects join on `_04_tmp1` |
| `_05_tmp4` index | — | 0.03 | 0.02 | index build cost |
| `_05` | **6.1** | **6.8** | **6.0** | SPATIAL_JOIN on `_05_tmp4` |

**Result: no improvement at any site. The `_04_tmp1` RTREE is net negative.**

- `_04_tmp2` went from 50.3s → 57.3s with the index: 0.9s build cost plus a slower join,
  because DuckDB already rewrites `JOIN … ON ST_Intersects(…)` to its internal
  `SPATIAL_JOIN` operator with its own temporary spatial index. An explicit RTREE creates
  a second index the planner must consider.
- `_02a`/`_02b`: at the time of this experiment, lines used LATERAL subqueries that
  evaluate as correlated loops; DuckDB cannot use a table-level RTREE inside them. The
  index is built and never probed. The current bbox-self-join form (see above) plans as
  `PIECEWISE_MERGE_JOIN` with explicit scalar predicates, also bypassing any RTREE.
- `_05`: times of 6.1s / 6.8s / 6.0s across none/merge/all are noise. The RTREE on
  `_05_tmp4` provided no measurable benefit.
- `_05_tmp2` NOT EXISTS filter against `_01`: indistinguishable across configs.

**The `_05_tmp4` RTREE has been removed.** The structural improvement in `_05_merge.py` is
materializing `_05_tmp4` as a real table — that decouples ST_Node/ST_Polygonize working
memory from the subsequent SPATIAL_JOIN regardless of whether any index exists on it.
The index itself was always noise.

---

## `ST_MemUnion_Agg` experiment

`ST_MemUnion_Agg` merges one geometry at a time rather than collecting the full set, trading speed for lower peak memory. Tested replacing every `ST_Union_Agg` call in the pipeline (Chile admin3, 1 thread, full run):

| Query | Function | RSS | Time | Notes |
| ----- | -------- | --- | ---- | ----- |
| `_02a` / `_02b` (lines, LATERAL neighbor union) | `ST_MemUnion_Agg` | 6,081 MB | 53s | was 6,311 MB / ~36s |
| `_03a` (points, global endpoint union) | `ST_MemUnion_Agg` | 1,476 MB | 8.7s | was 1,744 MB / ~3.5s |
| `_04` (voronoi, cell union by fid) | `ST_Union_Agg` | 3,985 MB | 166s | was 135s; `ST_MemUnion_Agg` killed |
| `check_gaps` (outputs, final polygon union) | `ST_MemUnion_Agg` | 5,282 MB | 8.9s | was ~2s |

**Result: `ST_MemUnion_Agg` is only viable where the union set per invocation is small.**

- **`_02a`/`_02b` (lines)**: each LATERAL neighbor union covered 3–10 geometries. Memory dropped 3.7% (6,311 → 6,081 MB); time rose ~47% (36s → 53s). Marginal tradeoff — kept at the time because lines was not the pipeline bottleneck and the memory direction was correct. (Superseded by the bbox-self-join rewrite, which removes LATERAL entirely; `ST_Union_Agg` is retained inside the new self-join's GROUP BY.)
- **`_03a` (points)**: global union of all exterior line segments' buffered endpoints. With `ST_MemUnion_Agg`, each segment is merged into a growing geometry one at a time — O(n²) as the accumulated shape grows. Time ~2.5× slower (3.5s → 8.7s) with no significant memory benefit.
- **`_04` (voronoi)**: each `fid` group contains hundreds to thousands of Voronoi cells. Each incremental merge grows the accumulated geometry, making later merges progressively more expensive. The query ran far beyond 135s and was killed. **`ST_Union_Agg` restored.**
- **`check_gaps` (outputs)**: union of all final polygons (~355 for Chile). Time rises from ~1s to 8.9s with no memory benefit.

**Conclusion**: `ST_MemUnion_Agg` reverted everywhere. No site offered a memory reduction large enough to affect pipeline feasibility, and the speed costs were disproportionate — especially `_04` (killed), `check_gaps` (9×), and `_03a` (2.5×). `ST_Union_Agg` is the correct choice throughout.
