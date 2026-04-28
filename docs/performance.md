# Performance Notes

Benchmarks and analysis for memory-constrained deployment (DuckDB-WASM, Docker).

Machine: Apple Silicon, macOS, 4 physical cores.

---

## Memory profiling methodology

**RSS peak is the primary metric** for Docker/WASM sizing. `duckdb_memory()` is unreliable
in both directions:

- **Undercounts GEOS working memory**: `ST_VoronoiDiagram`, `ST_Node`, `ST_Polygonize`
  allocate through GEOS's own heap — completely invisible to DuckDB's allocator tracking.
  For Chile `_04_tmp1`, this gap is ~2 GB (2.6 GB duckdb vs 4.6 GB RSS).
- **Overcounts when spilling**: the DuckDB buffer pool counts pages it has spilled to the
  `.duckdb` file as still "allocated". For Chile `_05`, this inflated the duckdb peak by
  ~2.5 GB (8.1 GB duckdb vs 5.5 GB RSS).

The `--profile` flag logs `rss peak` (from `psutil.Process().memory_info().rss`, sampled
every 50 ms) alongside `duckdb delta/total` for table-accumulation context.

---

## Thread-scaling benchmarks

### Chile admin3 (`chl_admin3.parquet`) — RSS peak per query, 4 threads

| Query | RSS peak | Notes |
| ----- | -------- | ----- |
| `_01` | 723 MB | reprojection |
| `_02a` | 3,817 MB | exterior boundary lines |
| `_02b` | **4,892 MB** | interior shared lines — first 4 GB breach |
| `_04_tmp1` | **4,597 MB** | Voronoi point collection (GEOS heap not in duckdb) |
| `_04_tmp2` | 3,120 MB | Voronoi polygon construction |
| `_05` | 5,500 MB | SPATIAL_JOIN merge |
| CHECKPOINT | **6,111 MB** | pipeline peak — flushing DuckDB file |
| COPY output | 5,414 MB | writing final Parquet |

**Pipeline peak: ~6.1 GB at 4 threads.** First exceeds 4 GB at `_02b` (interior lines).

### Findings

**Memory increases with thread count.** The dominant consumer is the shared DuckDB buffer
pool (materialized tables: `_01`, `_02b`, `_04`, etc.). More threads keep more pipeline
stages active simultaneously. Previous whole-run RSS benchmarks (measured with
`/usr/bin/time -l` on an older code version) showed Chile going from 8.4 GB at 1 thread
to 10.1 GB at 4 threads (+20%). Per-query RSS data for 2 and 1 threads has not yet been
collected with the current profiler.

**Speed gains are modest and front-loaded.** Chile: 6% at 2 threads, 15% at 4. Gains are
bounded by single-threaded GEOS operations (`ST_VoronoiDiagram`, `ST_Node`,
`ST_Polygonize`) — threads only help the surrounding DuckDB machinery (aggregations,
joins, scans). `_04_tmp2` (Voronoi polygon construction) shows near-linear thread scaling:
51s → 73s → 98s at 4 → 2 → 1 threads.

**Recommendation for 4 GB Docker**: Chile exceeds 4 GB at current 4-thread settings.
`--threads=1` is expected to reduce peak by ~20% based on prior whole-run measurements,
which would bring Chile to ~5 GB — still over the 4 GB target. Reducing memory below 4 GB
for Chile requires pipeline changes (earlier table drops, chunking, or reduced point
density via `--distance`).

---

## Pipeline phase profiles

RSS peak per phase for Chile admin3 at 4 threads:

| Phase       | Module       | RSS Peak   | Wall time   | Main bottleneck                                     |
| ----------- | ------------ | ---------- | ----------- | --------------------------------------------------- |
| Input       | `inputs.py`  | 723 MB     | Fast        | I/O                                                 |
| Lines       | `lines.py`   | 4,892 MB   | Moderate    | LATERAL join O(n × neighbors); GEOS line extraction |
| Points      | `points.py`  | ~1,900 MB  | Fast        | Interpolation                                       |
| **Voronoi** | `voronoi.py` | **4,597 MB** | Slow     | `ST_VoronoiDiagram` — GEOS, single-threaded         |
| **Merge**   | `merge.py`   | **6,111 MB** | Moderate | CHECKPOINT after `_05` SPATIAL_JOIN                 |
| Outputs     | `outputs.py` | ~5,400 MB  | Moderate    | `ST_Union_Agg` in topology checks                   |

**Voronoi** (`_04_tmp1`): `ST_VoronoiDiagram(ST_Collect(list(geom)))` materializes the
entire point cloud as a single GEOS GeometryCollection before computing. This GEOS heap is
invisible to `duckdb_memory()` — the 4.6 GB RSS peak vs 2.6 GB duckdb peak represents ~2
GB of hidden working memory. The retry/doubling-distance mechanism in `attempt.py` is the
safety valve: it backs off from 10M points until the operation fits in available memory.

**Lines** (`_02b`) is the first 4 GB breach in the pipeline. Line extraction involves
complex GEOS operations across all polygon boundaries simultaneously.

**CHECKPOINT** is the pipeline RSS peak. After the merge stage, DuckDB flushes dirty pages
from the buffer pool to the `.duckdb` file, briefly holding both the in-memory buffer and
the write buffers before releasing.

**Outputs topology checks**: `check_overlaps` is a self-join that could degrade to O(n²)
pairs without a spatial index, but DuckDB's `SPATIAL_JOIN` rewrite handles non-overlapping
polygon sets cheaply via bounding-box rejection. `check_gaps` runs `ST_Union_Agg` on all
final polygons — the most expensive single query in the outputs phase.

---

## `get_connection` settings

| Setting                            | Effect                                                                                                                                                                                            |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOAD spatial`                     | One-time extension load. No ongoing effect.                                                                                                                                                       |
| `enable_progress_bar = false`      | No memory or performance effect. Suppresses terminal noise.                                                                                                                                       |
| `geometry_always_xy = true`        | No memory or performance effect. Correctness: forces (lon, lat) coordinate order regardless of CRS definition. Required for correct EPSG:4326 output.                                             |
| `preserve_insertion_order = false` | **Free win.** Removes sequence-tracking overhead from every intermediate buffer and eliminates the reorder pass after parallel aggregations. Workers emit chunks immediately rather than queuing. |
| `threads = N`                      | Primary memory dial. RSS peak scales with thread count — more threads keep more pipeline stages active simultaneously. See benchmarks above.                                                      |

**`memory_limit` is unset** (defaults to 80% of system RAM). On a dev machine this is
fine; in a Docker container DuckDB doesn't know it's constrained and will allocate freely
until the OOM killer fires. For Docker, set `memory_limit` explicitly (e.g. `'1500MB'` in
a 2 GB container) so DuckDB can spill to disk rather than crash.

---

## Merge stage memory profile (Chile, 4 threads)

Full-pipeline RSS peaks for the merge stage queries. Note: these include the buffer pool
from all prior-stage tables (`_01`, `_02b`, `_04`, etc.) that are resident in memory, so
they are higher than isolated `--stage=merge` measurements.

| Query      | RSS peak  | Notes                                                       |
| ---------- | --------- | ----------------------------------------------------------- |
| `_05_tmp1` | 2,782 MB  | `ST_Within` anti-join against `_01`; SPATIAL_JOIN triggered |
| `_05_tmp2` | 2,839 MB  | Per-segment bbox pre-filter; see below                      |
| `_05_tmp3` | 3,345 MB  | `ST_Node` + `ST_Polygonize`                                 |
| `_05`      | 5,500 MB  | `SPATIAL_JOIN` — dominant cost                              |

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

After `ST_Polygonize`, Chile produces 355 cells averaging 2450 points each. The
`LEFT JOIN ... ON ST_Within(p.pt, c.geom)` triggers DuckDB's `SPATIAL_JOIN` operator
across 10,650 polygon part centroids (from `ST_Dump` on `_01`) vs these cells.

The `duckdb_memory()` peak for `_05` (~8 GB) was an overcount — the buffer pool includes
pages already spilled to the `.duckdb` file. Actual RSS peak is **5.5 GB**.

**Thread count is the primary lever**: isolated measurements (fresh connection, fewer
tables in buffer pool) show ~3× reduction from 4 threads to 1 thread for the SPATIAL_JOIN
itself, though full-pipeline RSS impact of thread count has not yet been measured with the
current profiler.

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
