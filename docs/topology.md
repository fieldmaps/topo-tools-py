# Topology Reference

## DuckDB vs `gdal vector clean-coverage`

The pipeline previously called `gdal vector clean-coverage` (GEOS `GEOSCoverageSimplify`/repair) at the inputs and merge stages. It has been removed. This section records what DuckDB can and cannot replicate, and the approach that was chosen.

### What DuckDB spatial exposes

| Function | Purpose |
|---|---|
| `ST_CoverageInvalidEdges_Agg` | Detects edges that don't match between adjacent polygons (validation only, no repair) |
| `ST_CoverageSimplify_Agg` | Topology-safe simplification (does not fix gaps or overlaps) |
| `ST_CoverageUnion_Agg` | Fast union for already-valid coverages (crashes on invalid input) |
| `ST_ReducePrecision` | Snaps vertices to a grid — makes edge mismatch worse when applied to only one layer |
| `ST_Node` | Computes all intersection points between a collection of lines, adding them as shared vertices |
| `ST_Polygonize` | Builds polygons from a planar noded edge network |
| `ST_MemUnion_Agg` | Memory-efficient union aggregate |

There is **no `ST_CoverageClean` or `ST_Snap`**. The GEOS coverage repair functions are not exposed.

### Why the naive approach creates gaps

The previous merge used `ST_Difference(voronoi_cell, ST_Union_Agg(nearby_originals))` per cell. This recomputed the original polygon boundary independently for each Voronoi cell. GEOS floating-point arithmetic produces slightly different crossing-point coordinates each time, creating sub-nanometer seam gaps that appear as visible diagonal lines in QGIS.

Applying `ST_ReducePrecision` to only the extension pieces (not originals) makes the problem **worse**: it snaps extension vertices to a grid that doesn't align with the original polygon coordinates, increasing mismatches.

### The solution: `ST_Node` + `ST_Polygonize`

`merge.main` now:

1. Collects **all original polygon boundaries** (`ST_Boundary` of `_01`) and **all Voronoi cell boundaries** (`ST_Boundary` of `_04`) into one edge set.
2. Calls `ST_Node` on the combined edge set — every crossing point (where a Voronoi boundary crosses an original polygon edge) becomes a shared vertex in both geometries simultaneously. No crossing point is ever computed twice.
3. Calls `ST_Polygonize` on the noded edges — produces a clean planar partition of the entire extent with no gaps or overlaps.
4. Assigns each piece to a `fid` via `ST_PointOnSurface` + point-in-polygon: original polygon assignment takes priority (preserving authoritative boundaries exactly), complement pieces fall back to the enclosing Voronoi cell.
5. Unions pieces by `fid`.

This produces **0 gaps, 0 overlaps, 0 `ST_CoverageInvalidEdges`** on all tested datasets. Original polygon vertex coordinates are never modified — the noding only adds collinear intermediate vertices where Voronoi edges cross original polygon edges, which is geometrically identical.

### Topology checks (`topology.py`)

Both a **strict** and an **area-based** check are run and compared:

| Check | Strict | Area-based (authoritative) |
|---|---|---|
| Overlaps | `ST_CoverageInvalidEdges_Agg IS NOT NULL` | `ST_Area(ST_Intersection) > 1e-10` |
| Gaps | `ST_NumInteriorRings(ST_Union_Agg) > 0` | `ST_Area(ST_Difference(extent, union)) > 1e-10` |

When the two disagree, a `WARNING` is logged with both values. The run only fails on the area-based result. `AREA_EPSILON = 1e-10` (≈ 0.1 m²) is the threshold below which a discrepancy is treated as a floating-point artifact rather than a real topology error.

---

## DuckDB 1.5.2 `SPATIAL_JOIN` Memory Reservation Bug

DuckDB 1.5.2's `SPATIAL_JOIN` operator pre-allocates approximately **1× physical RAM** as a virtual memory spill reservation before executing, regardless of actual data size. The default `memory_limit` of 80% RAM falls below this threshold on most machines, causing an immediate OOM error even when the join touches only ~100 MB of real data.

**Symptom**: The OOM message reads `"failed to allocate data of size X MiB (Y GiB/Y GiB used)"` where Y equals the `memory_limit` exactly. `duckdb_memory().memory_usage_bytes` shows only 60–100 MB — the two tracking systems are independent. The budget is exhausted by the reservation, not real data.

**What triggers `SPATIAL_JOIN`**: Any `ST_Within` / `ST_Contains` predicate in a JOIN. DuckDB's optimizer always rewrites to `SPATIAL_JOIN` — correlated subqueries, `LATERAL` joins, and batching all produce the same plan.

**Workaround** (implemented in `utils.spatial_join_memory`): The reservation is a virtual address claim — no physical pages are mapped. Any `memory_limit` above the reservation threshold passes the check. Setting the limit to `'999GB'` always exceeds the reservation on any real machine; restoring the original value afterward is cheap and platform-independent.

```python
@contextmanager
def spatial_join_memory(conn):
    orig = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    conn.execute("SET memory_limit = '999GB'")
    try:
        yield
    finally:
        conn.execute(f"SET memory_limit = '{orig}'")
```

Used in `merge.main` around the two `ST_Within` spatial joins.

**Note**: R-tree indexes (`CREATE INDEX ... USING RTREE (geom)`) are required before the spatial joins for efficient probing. May be fixed in DuckDB versions after 1.5.2 — re-test if upgrading.
