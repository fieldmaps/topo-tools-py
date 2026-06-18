# Topology Reference

## DuckDB vs `gdal vector clean-coverage`

The pipeline previously called `gdal vector clean-coverage` (GEOS `GEOSCoverageSimplify`/repair) at the inputs and merge stages. It has been removed. This section records what DuckDB can and cannot replicate, and the approach that was chosen.

### What DuckDB spatial exposes

| Function                      | Purpose                                                                                        |
| ----------------------------- | ---------------------------------------------------------------------------------------------- |
| `ST_CoverageInvalidEdges_Agg` | Detects edges that don't match between adjacent polygons (validation only, no repair)          |
| `ST_CoverageSimplify_Agg`     | Topology-safe simplification (does not fix gaps or overlaps)                                   |
| `ST_CoverageUnion_Agg`        | Fast union for already-valid coverages (crashes on invalid input)                              |
| `ST_ReducePrecision`          | Snaps vertices to a grid — makes edge mismatch worse when applied to only one layer            |
| `ST_Node`                     | Computes all intersection points between a collection of lines, adding them as shared vertices |
| `ST_Polygonize`               | Builds polygons from a planar noded edge network                                               |
| `ST_MemUnion_Agg`             | Memory-efficient union aggregate                                                               |

`ST_CoverageClean` is available as of DuckDB spatial 1.5.3 (used by the `clean` stage). `ST_Snap` is still not exposed.

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

### Topology checks (`checks.py`)

Both a **strict** and an **area-based** check are run and compared:

| Check    | Strict                                    | Area-based (authoritative)                      |
| -------- | ----------------------------------------- | ----------------------------------------------- |
| Overlaps | `ST_CoverageInvalidEdges_Agg IS NOT NULL` | `ST_Area(ST_Intersection) > 1e-10`              |
| Gaps     | `ST_NumInteriorRings(ST_Union_Agg) > 0`   | `ST_Area(ST_Difference(extent, union)) > 1e-10` |

When the two disagree, a `WARNING` is logged with both values. The run only fails on the area-based result. `AREA_EPSILON = 1e-10` (≈ 0.1 m²) is the threshold below which a discrepancy is treated as a floating-point artifact rather than a real topology error.

---

## `_05_tmp3` Inner-Line Filtering

`_05_tmp3` holds the Voronoi extension lines that are unioned with the original polygon boundaries in the final `ST_Node + ST_Polygonize` step. It must contain only lines in the **extension zone** (outside the original polygon union) — inner Voronoi lines (those running through the interior of the polygon union) must be removed, or they create spurious cells inside original polygons after polygonization.

### Classification rule

A section segment is **inner** if its representative point lies within any original polygon. It is **outer** otherwise. Segments whose middle briefly crosses a polygon boundary but whose endpoints are outside are treated as outer — this is an accepted edge case where the downstream polygonization handles any sliver gracefully.

### `ST_PointOnSurface` is the right test

`ST_PointOnSurface` on a linestring returns the **midpoint by length** (deterministic, not random, not the centroid). This is more robust than testing endpoints because:

- **Narrow-notch edge case**: a V-shaped crack in the polygon boundary can cause one endpoint of an outer line to land inside the polygon, even though the line itself passes through the gap and is correctly an extension line. The midpoint lands in the exterior portion and the line is kept.
- **Crossing-at-midpoint edge case**: a line whose middle crosses into a polygon at exactly the midpoint would be incorrectly removed, but this is rare and less harmful than removing real extension lines.

### Final implementation

```sql
WHERE NOT EXISTS (
    SELECT 1 FROM "{name}_01" p
    WHERE ST_Within(ST_PointOnSurface(s.geom), p.geom)
)
```

This replaces the original `ST_Union_Agg(geom) FROM _01` union approach. The union materialised a large merged polygon in memory — expensive and problematic for DuckDB-WASM and memory-limited Docker. The anti-join tests the midpoint against individual polygon rows, allowing DuckDB to short-circuit as soon as one containing polygon is found.

### Approaches ruled out

| Approach                                        | Reason rejected                                                                                                                                                                                |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ST_Union_Agg(_01)`                             | Materialises large intermediate geometry; expensive in WASM/Docker                                                                                                                             |
| `ST_ConvexHull(_01)`                            | Fast but incorrect for concave geographies (e.g. Chile coastline)                                                                                                                              |
| `ST_Polygonize(_02)`                            | Profiled as more expensive than the union                                                                                                                                                      |
| `is_extension` flag on Voronoi cells            | All Voronoi cells straddle the interior/extension boundary — seed points are on exterior polygon edges, so cells spread both inward and outward. No cell is purely interior or purely exterior |
| Endpoint test (`ST_StartPoint` / `ST_EndPoint`) | Fails for the narrow-notch edge case described above                                                                                                                                           |

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
