# Performance Notes

Benchmarks and analysis for memory-constrained deployment (DuckDB-WASM, Docker).

Machine: Apple Silicon, macOS, 10 logical cores.

---

## Memory profiling methodology

**RSS peak is the primary metric** for Docker/WASM sizing. `duckdb_memory()` is unreliable
in both directions:

- **Undercounts GEOS working memory**: `ST_VoronoiDiagram`, `ST_Node`, `ST_Polygonize`
  allocate through GEOS's own heap — completely invisible to DuckDB's allocator tracking.
  For Chile `_04_tmp1` at 1 thread, this gap is ~5.4 GB (1.0 GB duckdb vs 6.4 GB RSS).
- **Overcounts when spilling**: the DuckDB buffer pool counts pages it has spilled to the
  `.duckdb` file as still "allocated". For Chile `_05`, this inflated the duckdb peak by
  ~2.5 GB (8.1 GB duckdb vs 5.5 GB RSS).

The `--profile` flag logs `rss peak` (from `psutil.Process().memory_info().rss`, sampled
every 50 ms) alongside `duckdb delta/total` for table-accumulation context.


---

## Pipeline phase profiles

RSS peak per phase for Chile admin3 at 1 thread:

| Phase       | Module       | RSS Peak     | Wall time | Main bottleneck                                     |
| ----------- | ------------ | ------------ | --------- | --------------------------------------------------- |
| Input       | `inputs.py`  | 670 MB       | ~1s       | I/O                                                 |
| Lines       | `lines.py`   | 5,615 MB     | ~40s      | LATERAL join O(n × neighbors); GEOS line extraction |
| Points      | `points.py`  | 1,631 MB     | ~8s       | Interpolation                                       |
| **Voronoi** | `voronoi.py` | **6,370 MB** | ~149s     | `ST_VoronoiDiagram` — GEOS, single-threaded         |
| **Merge**   | `merge.py`   | 5,488 MB     | ~12s      | `_05` SPATIAL_JOIN                                  |
| Outputs     | `outputs.py` | ~5,037 MB    | ~4s       | COPY                                                |

**Voronoi** (`_04_tmp1`) is the pipeline peak at ~6.4 GB. `ST_VoronoiDiagram(ST_Collect(list(geom)))` materializes the
entire point cloud as a single GEOS GeometryCollection before computing. This GEOS heap is
invisible to `duckdb_memory()` — the 6.4 GB RSS peak vs ~1.0 GB duckdb total represents ~5.4
GB of hidden working memory. The retry/doubling-distance mechanism in `attempt.py` is the
safety valve: it backs off from 10M points until the operation fits in available memory.

**Lines** (`_02b`) is the first 4 GB breach in the pipeline. Line extraction involves
complex GEOS operations across all polygon boundaries simultaneously.

**Outputs topology checks**: `check_overlaps` is a self-join that could degrade to O(n²)
pairs without a spatial index, but DuckDB's `SPATIAL_JOIN` rewrite handles non-overlapping
polygon sets cheaply via bounding-box rejection. `check_gaps` runs `ST_Union_Agg` on all
final polygons — the most expensive single query in the outputs phase.

---

## Thread-scaling benchmarks (Chile admin3)

| threads    | pipeline peak RSS | `_04_tmp3` time | `_02b` time | `_05` time | total time |
| ---------- | ----------------- | --------------- | ----------- | ---------- | ---------- |
| 1          | 6,370 MB          | 149.1s          | 21.9s       | 6.8s       | ~362s      |
| 2          | 7,237 MB          | 144.2s          | 18.7s       | 7.1s       | ~322s      |
| 4          | 8,010 MB          | 147.6s          | 18.2s       | 6.9s       | ~289s      |
| unset (10) | **8,182 MB**      | **136.2s**      | **18.0s**   | **5.9s**   | **~275s**  |

Pipeline peak is `_04_tmp1` (Voronoi point collection) at all thread counts. Memory scales
+28% from 1→10 threads, driven by buffer pool pressure from concurrent pipeline stages.
Speed gains are modest (~24% total time from 1→10 threads); the bottleneck `_04_tmp3`
(Voronoi polygon construction, single-threaded GEOS) is nearly flat across all settings.
Parallelism helps primarily in line extraction (`_02a`/`_02b`) and the SPATIAL_JOIN (`_05`).

For memory-constrained deployments: `--threads=1` gives the lowest peak at ~6.4 GB. Still
above a 4 GB Docker target — reducing below that requires pipeline changes (chunking or
reduced point density via `--distance`).

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

## Merge stage memory profile (Chile)

Full-pipeline RSS peaks for the merge stage queries. Note: these include the buffer pool
from all prior-stage tables (`_01`, `_02b`, `_04`, etc.) that are resident in memory, so
they are higher than isolated `--stage=merge` measurements.

| Query      | RSS peak | Notes                                                         |
| ---------- | -------- | ------------------------------------------------------------- |
| `_05_tmp1` | 3,835 MB | `ST_Within` anti-join against `_01`; SPATIAL_JOIN triggered   |
| `_05_tmp2` | 3,848 MB | Per-segment bbox pre-filter; see below                        |
| `_05_tmp3` | 4,119 MB | `ST_Node` + `ST_Polygonize`; `_02b` dropped immediately after |
| `_05`      | 5,488 MB | `SPATIAL_JOIN` — dominant cost                                |

### `_05_tmp2`: `ST_ClosestPoint` against collected geometry

The original approach collected all of `_02b` (391 lines, 700K points) into a single
`MULTILINESTRING` and called `ST_ClosestPoint(collected, endpoint)` for each of 596
extension-line endpoints. This caused a **~6.8 GB** peak — GEOS allocates large internal
structures when processing a 700K-point geometry.

Fix: per-segment bbox pre-filter. For each endpoint, identify candidate `_02b` segments
via coordinate range comparison (`ST_XMin/XMax/YMin/YMax`), then call `ST_ClosestPoint`
only on matching segments. With `SNAP_TOLERANCE = 1e-8`, almost all of the 596 × 391 =
233K pairs are eliminated cheaply, and `ST_ClosestPoint` is called only on the ~370
matching pairs. Peak dropped to **~56 MB RSS delta**.

**Pattern to avoid**: `ST_ClosestPoint(ST_Collect(list(geom)), point)` on large tables.
Replace with a per-segment join filtered by bounding box.

### `_05`: SPATIAL_JOIN on complex cells

After `ST_Polygonize`, Chile produces 355 cells averaging 2,450 points each. The
`LEFT JOIN ... ON ST_Within(p.pt, c.geom)` triggers DuckDB's `SPATIAL_JOIN` operator
across 10,650 polygon part centroids (from `ST_Dump` on `_01`) vs these cells.
RSS peak at 1 thread is **5.5 GB**.

---

## RTREE index experiment

Tested adding explicit RTREE indexes on two additional join sites:

- `lines.py`: index on `_02_tmp1.geom` for the LATERAL neighbor join
- `voronoi.py`: index on `_04_tmp1.geom` for the point→Voronoi-cell fid assignment

Result: **no improvement; Chile was ~14 seconds slower.**

**Why they didn't help:**

- `voronoi.py` fid join: DuckDB already rewrites any `JOIN … ON ST_Intersects(…)` to its
  internal `SPATIAL_JOIN` operator, which builds its own temporary spatial index at query
  time. An explicit RTREE creates a second index the planner must consider, adding overhead
  without benefit.
- `lines.py` LATERAL join: DuckDB cannot use a table-level RTREE index inside a correlated
  LATERAL subquery. Each invocation is evaluated as a correlated loop, not a bulk spatial
  join, so the index is built and never probed. Pure overhead.

The RTREE on `_05_tmp3` in `merge.py` was added speculatively during the `_05` memory
investigation and also showed no improvement — consistent with the above. It is kept
because materializing `_05_tmp3` as a real table (required to create any index) is itself
the structural improvement: it decouples noding memory from the SPATIAL_JOIN, allowing
DuckDB to release noding working memory before the join starts.
