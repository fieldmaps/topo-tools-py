# Cleaning Reference

`clean` detects and fixes coverage defects (gaps, overlaps) in a single
polygon layer using `ST_CoverageClean`, and detects (but never auto-fixes)
slivers -- near-miss boundary mismatches. Ported from the sister JS app's
interactive Topology Cleaner tool
(`topo-tools-js/src/lib/tools/topology-cleaner/`); this doc covers what
changed in the port and why, and the `ST_CoverageClean` parameter semantics
the design depends on.

## Pipeline

1. **`_01_inputs`** -- reads and reprojects to EPSG:4326 via `extend`'s
   `read_and_reproject()` helper, **without** `extend`'s own auto-clean
   pre-check. This is deliberate: `clean`'s whole purpose is to detect
   defects in the *raw* input, so the detection stage needs to see them, not
   a table `ST_CoverageClean` has already silently rewritten.
2. **`_02_issues`** -- detects gap/overlap/sliver regions, writing one issues
   table (`{name}_02`).
3. **`_03_clean`** -- fixes gaps/overlaps via `ST_CoverageClean` (gated: a
   no-op copy if the input has no coverage violations at all).
4. **`_04_outputs`** -- validates overlaps are gone (hard gate), logs
   (never raises on) any gaps still unfilled by design, and exports both the
   cleaned dataset and the issues report.

## Why slivers are detect-only

The JS tool's git history documents an explicit reversal on this point
(commit `9e57932`, "slivers detection-only; remove snap and Changes
feature"): auto-snapping a near-miss sliver closed requires widening
`ST_CoverageClean`'s `snapping_distance` parameter, which re-nodes the
**whole** coverage -- not just the defect site -- silently perturbing
unrelated, already-correct geometry elsewhere in the file. That's an
unacceptable side effect for something running unattended in a batch
pipeline. Slivers are detected, reported in the issues file, and left
entirely alone; fixing them (re-digitizing the source, or manual editing in
QGIS/ArcGIS) is a human decision.

## `ST_CoverageClean` parameter semantics -- verified against upstream source

This mattered enough to check the actual GEOS/duckdb-spatial source rather
than assume JS's UI defaults translate directly (`duckdb-spatial`'s
`src/spatial/modules/geos/geos_module.cpp`/`geos_geometry.hpp`, and GEOS's
own `include/geos/coverage/CoverageCleaner.h`/`src/coverage/CoverageCleaner.cpp`):

- **`snapping_distance`** has a real computed auto-default:
  `extent_diameter / 1e8` (`computeDefaultSnappingDistance`).
  `setSnappingDistance(x)` is a no-op when `x < 0`, so passing `-1`
  (DuckDB's own default for an omitted argument) keeps this auto-computed
  value. `0` explicitly disables snapping; a positive value overrides it.
- **`gap_maximum_width` has NO computed auto-default** -- the C++ class
  member is hardcoded to `0.0` ("a width of zero prevents gaps from being
  merged"). `setGapMaximumWidth(x)` is also a no-op when `x < 0`, so an
  omitted/`-1` value leaves it at `0.0`, i.e. **no gap-filling at all**. This
  is why `extend`/`match`'s existing `coverage_clean()` calls (which never
  pass a positive `gap_max_width`) never fill gaps -- and why JS's "fill up
  to 2x the widest detected gap" was purely a client-side slider-seeding
  heuristic, not anything GEOS computes on its own.
- `ST_CoverageClean`'s gap-merge only fills **fully-enclosed** holes -- a
  ring of polygons surrounding missing area (a lake, a missing admin unit).
  An open "inlet" gap between two side-by-side, non-enclosing polygons is
  left untouched regardless of `gap_maximum_width` (confirmed empirically
  with an isolated 2-polygon fixture: identical output whether
  `gap_maximum_width` was `-1`, a tiny value, or 1 full degree). GEOS's own
  class doc says as much: "gaps which are not fully enclosed ... are not
  removed." This is also why `_02_issues.py`'s gap-detection query (interior
  rings of the whole-table union) misses open inlets -- they aren't
  fillable "gaps" by this tool's or GEOS's own definition, so they correctly
  surface as slivers instead (if within `--sliver-tolerance`).

## `--gap-width auto|all|<meters>`

- `auto` -- passes `-1.0` through, i.e. GEOS's real native default: **no
  gap-filling**. Matches `extend`/`match`'s existing unconfigured behavior.
- `all` (default when the flag is omitted) -- fills every gap the detection
  stage found. Computed as the widest detected gap's own width (`max(
  {name}_02.max_width_m WHERE kind='gap')`, already GEOS's own width metric:
  2x the gap polygon's `ST_MaximumInscribedCircle` radius) plus a small
  epsilon (`ALL_GAP_WIDTH_EPSILON_FACTOR` in `_constants.py`) so the widest
  gap itself clears the `<=` comparison, converted to degrees.
- A bare number is an explicit cap in meters, converted to degrees.

Since `ST_CoverageClean` only fills fully-enclosed gaps, a large `all`-mode
width computed from one real enclosed gap cannot accidentally swallow an
unrelated open-inlet sliver elsewhere in the same file, even though both are
compared against the same single `gap_maximum_width` value in one whole-table
call -- confirmed with the 8-fid fixture in `tests/test_clean.py` (a donut
gap, an overlap pair, and a sliver pair, all cleaned in one `ST_CoverageClean`
call): `--gap-width all` closes the donut's hole while leaving the sliver
pair's near-miss untouched.

## `--snap-tolerance auto|<meters>`

`auto` passes `-1.0` through (GEOS's real computed default). An explicit
value overrides it. This is a **noding-robustness knob only** -- per GEOS's
own doc comment, "a large snapping distance may introduce undesirable data
alteration" -- and must not be used to fix slivers (see above).

## `--sliver-tolerance`

Detection cutoff for `ST_CoverageInvalidEdges_Agg(geom, tolerance)`, in
meters (default `10.0`, matching JS's `SLIVER_TOL_DEFAULT_M`). `0` disables
sliver detection entirely. Already-detected gap/overlap regions are
subtracted (buffered by the same tolerance) from the raw invalid-edges result
before it's reported as slivers, so a genuine overlap or enclosed gap isn't
double-reported as a sliver too -- ported from JS's `sliverRegionsQuery`
(commit `7eb1967`, "dedup slivers against detected overlaps"). This
subtraction is not perfect at the fringes: a resolved overlap's edges can
leave a short residual line fragment just outside the buffered overlap
region, which will still show up as a (harmless, since review is manual
anyway) extra sliver row.

## Issues file schema

`key VARCHAR, kind VARCHAR, area_m2 DOUBLE, max_width_m DOUBLE, unit_a
BIGINT, unit_b BIGINT, geom GEOMETRY`. `kind` is `'gap'`, `'overlap'`, or
`'sliver'`. `area_m2`/`max_width_m` are `NULL` for slivers (a LineString has
no area or MIC-based width). `unit_a`/`unit_b` (the two fids involved) are
populated only for overlap rows. Geometry is intentionally mixed --
Polygon for gap/overlap, LineString for sliver -- which is why **Shapefile
is rejected** as an issues-file format (its single-geometry-type-per-file
constraint can't represent this; GeoPackage/GeoJSON/GeoParquet all handle
mixed geometry types fine).

## Units and meters-to-degrees conversion

All CLI-facing distance/area thresholds (`--gap-width`, `--snap-tolerance`,
`--sliver-tolerance`, and the internal `MIN_ISSUE_AREA_M2` noise floor) are
meters, converted to the EPSG:4326 degrees `ST_CoverageClean`/
`ST_CoverageInvalidEdges_Agg` actually take, using a latitude-aware factor
(`core/clean/_units.py`, ported from `units.ts`): one degree of longitude
shrinks by `cos(latitude)`, so conversions scale by the dataset's centroid
latitude (`ST_Y(ST_Centroid(ST_Extent_Agg(geom)))`), computed once per run.
Approximate over very large north-south extents -- adequate for a cleaning
tolerance, not for precise measurement.

## `check_gaps` is deliberately not reused as a hard gate

Unlike `extend`/`match`, `_04_outputs.py` does **not** call `extend`'s
`check_gaps()` on the final output -- `clean` can legitimately leave gaps
unfilled by design (`--gap-width auto`, or a numeric cap narrower than some
detected gap), so raising on any remaining gap would make the tool crash on
its own default-adjacent behavior. Instead it logs a warning with a count of
how many detected gaps remain uncovered, tested via `ST_Contains` against a
point on each gap's surface -- visibility for the issues file, not a failure
condition. `check_overlaps()` **is** reused as a hard gate: `ST_CoverageClean`
always resolves overlaps unconditionally, so any survivor means something
genuinely went wrong.

## Resilience

Each of the three detection queries (gap/overlap/sliver) and the fix stage's
`coverage_clean()` call are retried once against an `ST_ReducePrecision`-
reduced copy of the input on a GEOS topology failure (`REDUCED_PRECISION_DEG`
in `_constants.py`, ported from JS's `clean.ts`), then fall back to an empty
result for that one kind (logged) rather than raising -- consistent with
`match`'s "failed group is logged and dropped, not fatal" precedent, applied
per-detection-kind here instead of per-group.
