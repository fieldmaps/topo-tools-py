# Performance Notes

Benchmarks and analysis for memory-constrained deployment (DuckDB-WASM, Docker).

Machine: Apple Silicon, macOS, 4 physical cores. Measured with `/usr/bin/time -l` (peak RSS = maximum resident set size).

---

## Thread-scaling benchmarks

### Afghanistan admin2 (`afg_admin2_v01.parquet`)

| Threads | Wall time | Peak RSS |
|---------|-----------|----------|
| 1       | 2:14      | 3.47 GB  |
| 2       | 2:00      | 3.49 GB  |
| 4       | 2:02      | 3.52 GB  |

### Chile admin3 (`chl_admin3.parquet`)

| Threads | Wall time | Peak RSS |
|---------|-----------|----------|
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

| Phase | Module | Peak memory | Wall time | Main bottleneck |
|-------|--------|-------------|-----------|-----------------|
| Input | `inputs.py` | ~2× input size | Fast | I/O |
| Lines | `lines.py` | Low (1D geometries) | Moderate | LATERAL join O(n × neighbors) |
| Points | `points.py` | ~400 MB at 10M pts | Fast | Interpolation |
| **Voronoi** | `voronoi.py` | **2–5 GB** | Slow | `ST_VoronoiDiagram` — GEOS, single-threaded |
| **Merge** | `merge.py` | **500 MB – 1.5 GB** | **Longest** | `ST_Node` + `ST_Polygonize` — GEOS, single-threaded |
| Outputs | `outputs.py` | 300 MB – 1 GB | Moderate | `ST_Union_Agg` in topology checks |

**Voronoi** is the memory ceiling: `ST_VoronoiDiagram(ST_Collect(list(geom)))` materializes the entire point cloud as a single GEOS GeometryCollection before computing anything. For 10M points this is ~2–5 GB in GEOS heap — cannot be streamed or chunked. The retry/doubling-distance mechanism in `attempt.py` is the safety valve: it backs off from 10M points until the operation fits in available memory.

**Merge** is the wall-clock bottleneck. `ST_Node` on combined boundaries has the same collect-everything pattern. For Chile, most of the 6-minute runtime is here.

**Outputs topology checks**: `check_overlaps` is a self-join that could degrade to O(n²) pairs without a spatial index, but DuckDB's `SPATIAL_JOIN` rewrite handles non-overlapping polygon sets cheaply via bounding-box rejection. `check_gaps` runs `ST_Union_Agg` on all final polygons — the most expensive single query in the outputs phase.

---

## `get_connection` settings

| Setting | Effect |
|---------|--------|
| `LOAD spatial` | One-time extension load. No ongoing effect. |
| `enable_progress_bar = false` | No memory or performance effect. Suppresses terminal noise. |
| `geometry_always_xy = true` | No memory or performance effect. Correctness: forces (lon, lat) coordinate order regardless of CRS definition. Required for correct EPSG:4326 output. |
| `preserve_insertion_order = false` | **Free win.** Removes sequence-tracking overhead from every intermediate buffer and eliminates the reorder pass after parallel aggregations. Workers emit chunks immediately rather than queuing. |
| `threads = N` | Primary memory dial. Memory scales ~linearly with thread count. See benchmarks above. |

**`memory_limit` is unset** (defaults to 80% of system RAM). On a dev machine this is fine; in a Docker container DuckDB doesn't know it's constrained and will allocate freely until the OOM killer fires. For Docker, set `memory_limit` explicitly (e.g. `'1500MB'` in a 2 GB container) so DuckDB can spill to disk rather than crash.

---

## RTREE index experiment

Tested adding explicit RTREE indexes on two additional join sites:

- `lines.py`: index on `_02_tmp1.geom` for the LATERAL neighbor join
- `voronoi.py`: index on `_04_tmp1.geom` for the point→Voronoi-cell fid assignment

Result: **no improvement; Chile was ~14 seconds slower.**

**Why they didn't help:**

- `voronoi.py` fid join: DuckDB already rewrites any `JOIN … ON ST_Intersects(…)` to its internal `SPATIAL_JOIN` operator, which builds its own temporary spatial index at query time. An explicit RTREE creates a second index the planner must consider, adding overhead without benefit.
- `lines.py` LATERAL join: DuckDB cannot use a table-level RTREE index inside a correlated LATERAL subquery. Each invocation is evaluated as a correlated loop, not a bulk spatial join, so the index is built and never probed. Pure overhead.

The existing RTREEs in `merge.py` are kept because they are required to work around the DuckDB 1.5.2 `SPATIAL_JOIN` memory reservation bug, not for general speedup (see CLAUDE.md).
