# Topology Reference

## DuckDB vs `gdal vector clean-coverage`

The pipeline previously called `gdal vector clean-coverage` (GEOS `GEOSCoverageSimplify`/repair) at the inputs and merge stages. It has been removed. This section records what DuckDB can and cannot replicate, and the approach that was chosen.

## Merge: union + difference + whole-table `ST_CoverageClean`

`merge.main` produces `_05` by:

1. Unioning each fid's original geometry with its Voronoi extension (`_04`) minus a bbox-prefiltered union of nearby originals (per-fid `ST_Difference`, not one global union used as the operand — see "Why not a single global union" below).
2. Dissolving to one row per fid and reattaching original attributes.
3. Running a single whole-table `ST_CoverageClean` call (via the shared `coverage_clean` helper in `utils.py`, also used by `inputs.main`'s coverage-clean pass) to close the floating-point-scale seams that the independent per-fid `ST_Difference` calls leave behind — the same failure mode described below under "Why the naive approach creates gaps," now fixed by the native function instead of hand-rolled noding.

This replaced an earlier `ST_Node` + `ST_Polygonize` design (kept below for historical context) that existed only because DuckDB spatial had no native `ST_CoverageClean` at the time it was built — confirmed by recovering the original PostGIS implementation (`git show f6a3b67:app_postgis/merge.py`), which used exactly this union+difference+coverage-clean pattern (via PostGIS's `ST_CoverageClean(geom) OVER ()` window function) long before the DuckDB port. Byte-exact preservation of original polygon vertices is not a goal of the current design — `ST_CoverageClean` may shift any polygon's boundary, including ones that weren't touched by the union/difference step.

### Why not a single global union as the `ST_Difference` operand

Using `ST_Union_Agg(_01)` — a single dissolved reference geometry — as the second argument to `ST_Difference` for every fid individually OOMs outright at Chile scale: the union can hold millions of vertices, and GEOS pays that cost on every row (`failed to allocate data of size 16.0 MiB (12.7 GiB/12.7 GiB used)`, observed during development). The fix is the same bbox-prefiltered neighbor-union self-join pattern `_02_lines.py` already uses for exterior-edge extraction — join `_04` against per-part (not per-fid) bboxes of `_01`, since a single Chile fid's multipolygon bbox can span mainland to a remote island and defeat the prefilter if not exploded into parts first.

### Historical: why `ST_Node` + `ST_Polygonize` was used instead (now removed)

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

`ST_CoverageClean` is available as of DuckDB spatial 1.5.3 (used by `inputs.main`'s coverage-clean pass and by `merge.main`). `ST_Snap` is still not exposed.

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

### Topology checks (`_06_outputs.py`)

`_check_overlaps` (`ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL`, via `utils.has_coverage_violations`) and `_check_gaps` (`ST_NumInteriorRings(ST_Union_Agg(geom)) > 0`) run on the final `_05` table and raise `RuntimeError` on failure. Both unnest MultiPolygon rows into single-polygon parts first, so a coverage split into multiple parts (e.g. mainland + offshore islet) doesn't hide a real interior-ring gap. There is no separate area-based check or epsilon-based warning tier — these are the only two topology gates in the pipeline.

---

## DuckDB 1.5.2 `SPATIAL_JOIN` Memory Reservation Bug

DuckDB 1.5.2's `SPATIAL_JOIN` operator pre-allocates approximately **1× physical RAM** as a virtual memory spill reservation before executing, regardless of actual data size. The default `memory_limit` of 80% RAM falls below this threshold on most machines, causing an immediate OOM error even when the join touches only ~100 MB of real data.

**Symptom**: The OOM message reads `"failed to allocate data of size X MiB (Y GiB/Y GiB used)"` where Y equals the `memory_limit` exactly. `duckdb_memory().memory_usage_bytes` shows only 60–100 MB — the two tracking systems are independent. The budget is exhausted by the reservation, not real data.

**What triggers `SPATIAL_JOIN`**: Any `ST_Within` / `ST_Contains` predicate in a JOIN. DuckDB's optimizer always rewrites to `SPATIAL_JOIN` — correlated subqueries, `LATERAL` joins, and batching all produce the same plan.

**Current mitigation**: the pipeline avoids this class of bug entirely rather than working around it — no stage uses `ST_Within`/`ST_Contains` in a JOIN condition. Bbox-prefiltered self-/cross-joins with scalar predicates (`_02_lines.py`'s neighbor-union, `_05_merge.py`'s `_05_tmp2`) plan as `PIECEWISE_MERGE_JOIN` instead, which never triggers the reservation.

If a future stage genuinely needs a true `ST_Within`/`ST_Contains` join, the reservation is a virtual address claim (no physical pages mapped), so any `memory_limit` above the reservation threshold passes the check:

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

Don't reach for an explicit RTREE index instead — it was profiled as providing no measurable benefit once DuckDB's own `SPATIAL_JOIN` rewrite already builds its own temporary index (see `docs/performance.md`, "RTREE index experiment").

**Note**: May be fixed in DuckDB versions after 1.5.2 — re-test if upgrading.
