# Performance Notes

Benchmarks and analysis for memory-constrained deployment (DuckDB-WASM, Docker).

Machine: Apple Silicon, macOS, 4 physical cores. Measured with `/usr/bin/time -l` (peak RSS = maximum resident set size).

---

## Thread-scaling benchmarks

### Afghanistan admin2 (`afg_admin2_v01.parquet`)

| Threads | Wall time | Peak RSS |
| ------- | --------- | -------- |
| 1       | 2:14      | 3.47 GB  |
| 2       | 2:00      | 3.49 GB  |
| 4       | 2:02      | 3.52 GB  |

### Chile admin3 (`chl_admin3.parquet`)

| Threads | Wall time | Peak RSS |
| ------- | --------- | -------- |
| 1       | 7:09      | 8.41 GB  |
| 2       | 6:42      | 9.26 GB  |
| 4       | 6:06      | 10.05 GB |

### Findings

**Memory increases with thread count, not decreases.** The dominant memory consumer is the shared DuckDB buffer pool (materialized tables: points `_03`, Voronoi cells `_04`, noded boundaries). With more threads, more pipeline stages are active simultaneously, keeping more data in memory at peak. Chile goes from 8.4 GB at 1 thread to 10.1 GB at 4 threads (+20%).

**Speed gains are modest and front-loaded.** Afghanistan: 10% improvement from 1→2 threads, flat at 4. Chile: 6% at 2 threads, 15% at 4. Gains are bounded by single-threaded GEOS operations (`ST_VoronoiDiagram`, `ST_Node`, `ST_Polygonize`) — threads only help the surrounding DuckDB machinery (aggregations, joins, scans).

**Recommendation for memory-constrained Docker**: `--threads=1` saves ~20% memory vs the default of 4, at a ~15% speed penalty. Use `--threads=2` if ~10 GB is available and the speed matters.

---

## Pipeline phase profiles

Memory and time breakdown for Chile admin3 at 4 threads (the stress test):

| Phase       | Module       | Peak memory         | Wall time   | Main bottleneck                                     |
| ----------- | ------------ | ------------------- | ----------- | --------------------------------------------------- |
| Input       | `inputs.py`  | ~2× input size      | Fast        | I/O                                                 |
| Lines       | `lines.py`   | Low (1D geometries) | Moderate    | LATERAL join O(n × neighbors)                       |
| Points      | `points.py`  | ~400 MB at 10M pts  | Fast        | Interpolation                                       |
| **Voronoi** | `voronoi.py` | **2–5 GB**          | Slow        | `ST_VoronoiDiagram` — GEOS, single-threaded         |
| **Merge**   | `merge.py`   | **~8 GB (Chile)**   | **Longest** | `SPATIAL_JOIN` in `_05` — see merge profile below   |
| Outputs     | `outputs.py` | 300 MB – 1 GB       | Moderate    | `ST_Union_Agg` in topology checks                   |

**Voronoi** is the memory ceiling: `ST_VoronoiDiagram(ST_Collect(list(geom)))` materializes the entire point cloud as a single GEOS GeometryCollection before computing anything. For 10M points this is ~2–5 GB in GEOS heap — cannot be streamed or chunked. The retry/doubling-distance mechanism in `attempt.py` is the safety valve: it backs off from 10M points until the operation fits in available memory.

**Merge** is the wall-clock and memory bottleneck. `ST_Node` + `ST_Polygonize` are cheap in isolation (~270 MB each for Chile). The dominant cost is the `SPATIAL_JOIN` in `_05` — see merge profile below.

**Outputs topology checks**: `check_overlaps` is a self-join that could degrade to O(n²) pairs without a spatial index, but DuckDB's `SPATIAL_JOIN` rewrite handles non-overlapping polygon sets cheaply via bounding-box rejection. `check_gaps` runs `ST_Union_Agg` on all final polygons — the most expensive single query in the outputs phase.

---

## `get_connection` settings

| Setting                            | Effect                                                                                                                                                                                            |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOAD spatial`                     | One-time extension load. No ongoing effect.                                                                                                                                                       |
| `enable_progress_bar = false`      | No memory or performance effect. Suppresses terminal noise.                                                                                                                                       |
| `geometry_always_xy = true`        | No memory or performance effect. Correctness: forces (lon, lat) coordinate order regardless of CRS definition. Required for correct EPSG:4326 output.                                             |
| `preserve_insertion_order = false` | **Free win.** Removes sequence-tracking overhead from every intermediate buffer and eliminates the reorder pass after parallel aggregations. Workers emit chunks immediately rather than queuing. |
| `threads = N`                      | Primary memory dial. Memory scales ~linearly with thread count. See benchmarks above.                                                                                                             |

**`memory_limit` is unset** (defaults to 80% of system RAM). On a dev machine this is fine; in a Docker container DuckDB doesn't know it's constrained and will allocate freely until the OOM killer fires. For Docker, set `memory_limit` explicitly (e.g. `'1500MB'` in a 2 GB container) so DuckDB can spill to disk rather than crash.

---

## Merge stage memory profile (Chile, 4 threads)

Profiled with `--stage=merge --profile`. All peaks measured via `duckdb_memory()`.

| Query | Peak | Notes |
|---|---|---|
| `_05_tmp1` | ~620 MB | `ST_Within` anti-join against `_01`; SPATIAL_JOIN triggered |
| `_05_tmp2` | ~121 MB | Per-segment bbox pre-filter; see below |
| `_05_tmp3` | ~207 MB | `ST_Node` + `ST_Polygonize` in isolation |
| `_05` | ~8 GB | `SPATIAL_JOIN` — dominant cost |

### `_05_tmp2`: `ST_ClosestPoint` against collected geometry

The original approach collected all of `_02b` (391 lines, 700K points) into a single `MULTILINESTRING` and called `ST_ClosestPoint(collected, endpoint)` for each of 596 extension-line endpoints. This caused a **~6.8 GB** peak — GEOS allocates large internal structures when processing a 700K-point geometry.

Fix: per-segment bbox pre-filter. For each endpoint, identify candidate `_02b` segments via coordinate range comparison (`ST_XMin/XMax/YMin/YMax`), then call `ST_ClosestPoint` only on matching segments. With `SNAP_TOLERANCE = 1e-8`, almost all of the 596 × 391 = 233K pairs are eliminated cheaply, and `ST_ClosestPoint` is called only on the ~370 matching pairs. Peak dropped to **~121 MB**.

**Pattern to avoid**: `ST_ClosestPoint(ST_Collect(list(geom)), point)` on large tables. Replace with a per-segment join filtered by bounding box.

### `_05`: SPATIAL_JOIN on complex cells

After `ST_Polygonize`, Chile produces 355 cells averaging 2450 points each. The `LEFT JOIN ... ON ST_Within(p.pt, c.geom)` triggers DuckDB's `SPATIAL_JOIN` operator across 10,650 polygon part centroids (from `ST_Dump` on `_01`) vs these cells.

Isolated measurements (fresh connection, fewer tables in buffer pool):

| Approach | Peak |
|---|---|
| Single CTE, no RTREE | ~4341 MB |
| Materialized tables + RTREE, 4 threads | ~4138 MB |
| Materialized tables + RTREE, 1 thread | ~2243 MB |

**Pipeline peaks are higher than isolated measurements.** The DuckDB file holds many large tables (`_01`, `_02b`, `_04`, etc.) whose pages remain in the buffer pool during the join. In a full pipeline run, this adds several GB on top of the SPATIAL_JOIN's own allocation, pushing the peak to ~8 GB.

**RTREE index on cells**: added to `_05_tmp3` (required materialization of cells as a real table first, since CTEs cannot be indexed). No measurable improvement — consistent with the general RTREE finding below.

**Thread count is the primary lever**: isolated measurements show ~3× reduction from 4 threads to 1 thread. Full pipeline thread impact on Chile is documented in the thread-scaling benchmarks above.

---

## RTREE index experiment

Tested adding explicit RTREE indexes on two additional join sites:

- `lines.py`: index on `_02_tmp1.geom` for the LATERAL neighbor join
- `voronoi.py`: index on `_04_tmp1.geom` for the point→Voronoi-cell fid assignment

Result: **no improvement; Chile was ~14 seconds slower.**

**Why they didn't help:**

- `voronoi.py` fid join: DuckDB already rewrites any `JOIN … ON ST_Intersects(…)` to its internal `SPATIAL_JOIN` operator, which builds its own temporary spatial index at query time. An explicit RTREE creates a second index the planner must consider, adding overhead without benefit.
- `lines.py` LATERAL join: DuckDB cannot use a table-level RTREE index inside a correlated LATERAL subquery. Each invocation is evaluated as a correlated loop, not a bulk spatial join, so the index is built and never probed. Pure overhead.

The RTREE on `_05_tmp3` in `merge.py` was added speculatively during the `_05` memory investigation and also showed no improvement — consistent with the above. It is kept because materializing `_05_tmp3` as a real table (required to create any index) is itself the structural improvement: it decouples noding memory from the SPATIAL_JOIN, allowing DuckDB to release noding working memory before the join starts.
