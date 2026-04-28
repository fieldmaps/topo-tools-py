# Performance Notes

Benchmarks and analysis for memory-constrained deployment (DuckDB-WASM, Docker).

Machine: Apple Silicon, macOS, 10 logical cores.

---

## Memory profiling methodology

**RSS peak is the primary metric** for Docker/WASM sizing. `duckdb_memory()` is unreliable
in both directions:

- **Undercounts GEOS working memory**: `ST_VoronoiDiagram`, `ST_Node`, `ST_Polygonize`
  allocate through GEOS's own heap — completely invisible to DuckDB's allocator tracking.
  For Chile `_04_tmp1` at 1 thread, this gap is ~6.9 GB (0.9 GB duckdb vs 7.8 GB RSS).
- **Overcounts when spilling**: the DuckDB buffer pool counts pages it has spilled to the
  `.duckdb` file as still "allocated". For Chile `_05`, this inflated the duckdb peak by
  ~2.5 GB (8.1 GB duckdb vs 5.5 GB RSS).

The `--profile` flag logs `rss peak` (from `psutil.Process().memory_info().rss`, sampled
every 50 ms) alongside `duckdb delta/total` for table-accumulation context.


---

## Pipeline phase profiles

RSS peak per phase for Chile admin3 at 1 thread:

| Phase       | Module       | RSS Peak     | Wall time | Main bottleneck                                           |
| ----------- | ------------ | ------------ | --------- | --------------------------------------------------------- |
| Input       | `inputs.py`  | 680 MB       | ~1s       | I/O                                                       |
| Lines       | `lines.py`   | 2,708 MB     | ~28s      | Self-join bbox neighbor union + GEOS line extraction      |
| Points      | `points.py`  | 2,282 MB     | ~7s       | Interpolation + endpoint union                            |
| **Voronoi** | `voronoi.py` | **6,901 MB** | ~278s     | `ST_VoronoiDiagram` + fid join + `ST_Union_Agg`           |
| **Merge**   | `merge.py`   | 6,455 MB     | ~4s       | `ST_Node` + `ST_Polygonize` + `_05` `ST_Within` join      |
| Outputs     | `outputs.py` | 6,490 MB     | ~3s       | `check_gaps` `ST_Union_Agg` + COPY                        |

**Voronoi** (`_04_tmp1`) is the pipeline peak at ~6.9 GB. The stage has three steps:
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

## Thread-scaling benchmarks (Chile admin3)

| threads    | pipeline peak RSS | `_04_tmp2` time | `_04` time | `_02b` time | `_05` time | total time |
| ---------- | ----------------- | --------------- | ---------- | ----------- | ---------- | ---------- |
| 1          | **6,901 MB**      | 100.2s          | 141.3s     | 7.4s        | 1.8s       | ~320s      |
| unset (10) | 6,776 MB          | 56.3s           | 140.3s     | 7.5s        | 1.9s       | ~271s      |

Pipeline peak is `_04_tmp1` (Voronoi point collection + diagram) at all thread counts.

Key thread-sensitivity breakdown:
- `_04_tmp2` (fid assignment via `ST_Intersects`): 100s → 56s, 1.8× faster with more threads
- `_04` (`ST_Union_Agg` by fid, single-threaded GEOS): ~141s → ~140s, flat — the hard ceiling
- `_02b` (line extraction, bbox-self-join): 7.4s → 7.5s, no gain — `PIECEWISE_MERGE_JOIN` is single-threaded internally
- `_05` (`SPATIAL_JOIN` on `_05_tmp4` ST_Within): 1.8s → 1.9s, negligible

For memory-constrained deployments: `--threads=1` gives a similar peak (~6.9 GB) to
default threads. Both are above a 4 GB WASM/Docker target — reducing below that requires
pipeline changes (chunking or reduced point density via `--distance`).

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
predicates only**, then keys `_02a`/`_02b` off the resulting `_02_tmp2` table:

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

The same pattern applies in `merge.py` `_05_tmp1`: an explicit bbox prefilter
(`ST_X(pt)` vs `ST_XMin/XMax(p.geom)` etc.) on the `NOT EXISTS` subquery removes
`SPATIAL_JOIN` from that plan too. The remaining `SPATIAL_JOIN` site is `_05`'s
`LEFT JOIN ST_Within` against `_05_tmp4`.

---

## Merge stage memory profile (Chile)

Full-pipeline RSS peaks for the merge stage queries at 1 thread. Note: these include the
buffer pool from all prior-stage tables (`_01`, `_02b`, `_04`, etc.) that are resident in
memory, so they are higher than isolated `--stage=merge` measurements.

| Query      | RSS peak | Notes                                                              |
| ---------- | -------- | ------------------------------------------------------------------ |
| `_05_tmp1` | 3,711 MB | Extension line extraction (`ST_Difference` + bbox-prefiltered `NOT EXISTS` vs `_01`) |
| `_05_tmp2` | 3,727 MB | Endpoint snapping to `_02b`; see below                             |
| `_05_tmp3` | 4,250 MB | `ST_Node` + `ST_Polygonize`; `_02b` dropped immediately after      |
| `_05_tmp4` | 4,247 MB | One interior point per polygon part (from `ST_Dump` on `_01`)      |
| `_05`      | 6,455 MB | `LEFT JOIN ST_Within` (`SPATIAL_JOIN`) + nearest-neighbor fallback for orphan cells |

### `_05_tmp2`: `ST_ClosestPoint` against collected geometry

The original approach collected all of `_02b` (391 lines, 700K points) into a single
`MULTILINESTRING` and called `ST_ClosestPoint(collected, endpoint)` for each of 596
extension-line endpoints. This caused a **~6.8 GB** peak — GEOS allocates large internal
structures when processing a 700K-point geometry.

Fix: per-segment bbox pre-filter. For each endpoint, identify candidate `_02b` segments
via coordinate range comparison (`ST_XMin/XMax/YMin/YMax`), then call `ST_ClosestPoint`
only on matching segments. With `SNAP_TOLERANCE = 1e-8`, almost all of the 596 × 391 =
233K pairs are eliminated cheaply, and `ST_ClosestPoint` is called only on the ~370
matching pairs. RSS delta is ~38 MB (2.5s at 1 thread).

**Pattern to avoid**: `ST_ClosestPoint(ST_Collect(list(geom)), point)` on large tables.
Replace with a per-segment join filtered by bounding box.

### `_05`: SPATIAL_JOIN + nearest-neighbor fallback

After `ST_Polygonize`, Chile produces 355 cells. `_05_tmp4` materializes one interior
point per polygon part (from `ST_Dump` on `_01`). The `LEFT JOIN ST_Within` assigns each
cell to its source polygon; an unmatched fallback assigns orphan extension cells to the
nearest polygon by centroid distance. RSS peak at 1 thread is **6.7 GB** in ~1.7s.

---

## RTREE index experiment

Tested adding explicit RTREE indexes at every candidate spatial join site across the full
pipeline (Chile admin3, default threads). Three configurations:

- **none** — no RTREEs anywhere
- **merge** — RTREE only on `_05_tmp3` (former default)
- **all** — RTREEs on `_01`, `_02_tmp1`, `_04_tmp1`, and `_05_tmp3`

**Wall time (seconds) at key queries:**

| Query | none | merge | all | join type |
|---|---|---|---|---|
| `_02a` | 11.8 | 11.1 | 11.2 | LATERAL + ST_Intersects on `_02_tmp1` |
| `_02b` | 18.0 | 17.6 | 17.1 | LATERAL + ST_Intersects on `_02_tmp1` |
| `_04_tmp1` index | — | — | **0.9** | index build cost |
| `_04_tmp2` | **50.3** | **55.9** | **57.3** | ST_Intersects join on `_04_tmp1` |
| `_05_tmp3` index | — | 0.03 | 0.02 | index build cost |
| `_05` | **6.1** | **6.8** | **6.0** | SPATIAL_JOIN on `_05_tmp3` |

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
  `_05_tmp3` provided no measurable benefit.
- `_05_tmp1` NOT EXISTS filter against `_01`: indistinguishable across configs.

**The `_05_tmp3` RTREE has been removed.** The structural improvement in `merge.py` is
materializing `_05_tmp3` as a real table — that decouples ST_Node/ST_Polygonize working
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
