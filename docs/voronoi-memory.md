# Voronoi outlier investigation — RESOLVED

## Resolution summary

The collinearity degeneracy described below is fixed. `app/_03_points.py` now
decomposes each real boundary line into its own real vertex-to-vertex
segments (no geometry alteration) and caps interpolation on any segment
longer than `DISTANCE * MAX_POINTS_PER_SEGMENT` (config.py,
`MAX_POINTS_PER_SEGMENT = 100`) — this bounds the size of the largest
exactly-collinear point cluster fed to `ST_VoronoiDiagram`, independent of
that segment's raw length. Confirmed: Chad's `ST_VoronoiDiagram` step
296–387s → ~1s; full pipeline 1115s → ~20s. Algeria (previously only assumed
to share Chad's mechanism) confirmed and fixed the same way; full pipeline →
~30s. Chile, Indonesia, and Philippines all confirmed unaffected/working.

`app/attempt.py` additionally now computes a per-file starting `DISTANCE`:
`effective_distance = MAX(MIN(DISTANCE, natural_res), total_exterior_length /
target_point_budget)`. `natural_res` (median real segment length) lets files
with genuinely finer source detail than the default (e.g. Philippines) start
sharper instead of losing that detail to a coarser default. This is fully
independent of the segment cap above — `MAX_POINTS_PER_SEGMENT` still
protects against collinearity degeneracy no matter what this formula picks.

Three real bugs were found and fixed during implementation, not just the
core algorithm: (1) `ST_PointN`-based segment decomposition is O(n²) and
OOMs almost instantly at scale — fixed via `ST_Points`/`ST_Dump`/`LAG()`;
(2) differencing generated points against the shared-boundary zone per
segment instead of per fid caused a ~240x call-count blowup that OOM'd
Indonesia — fixed by aggregating to one multipoint per fid first; (3)
unconditionally guaranteeing one point per real segment created a floor
equal to the file's raw vertex count, which broke Philippines (13M real
vertices in its exterior boundary alone) — fixed by only capping segments
that are actually long, and letting normal segments fall back to the
original whole-line resampling formula.

## Memory ceiling: `--memory-gb` and the per-file point budget

`target_point_budget` above is not a hardcoded constant — an earlier version
used a single value (`TARGET_POINT_BUDGET = 5_500_000`) calibrated once
against `chl_admin3`'s RSS-peak behavior on an unconstrained dev machine.
That calibration point was never actually safe: tested inside a real
`--memory=4g --memory-swap=4g` (no swap) Docker container — the real
deployment constraint, not RSS inference — Chile OOM'd at exactly that
"known-good" distance. `docker inspect`'s `.State.OOMKilled` confirmed this
is a hard kernel SIGKILL, not a catchable `DuckDBError` — `attempt.py`'s
retry-doubling loop never runs in that case, so the budget must avoid the
ceiling in the first place, not retry after crossing it.

The corrected model has two DISTANCE-independent terms plus one
DISTANCE-dependent term, fitted from probes run inside the real container:

1. **Segment decompose+remerge cost** (`_REMERGE_BYTES_PER_RAW_SEGMENT` in
   `config.py`) — scales with each file's own raw vertex count, *before* any
   DISTANCE is applied. Measured: `idn_admin3` 2.49M segments → 1788MB;
   `chl_admin3` 3.23M segments → 2544MB (~787 B/segment, used as the
   constant). `phl_admin3` (13.07M segments) OOM'd on this step alone,
   confirming no DISTANCE value can rescue a file whose raw vertex count
   alone exceeds the budget — `attempt.py` now fails fast on this case
   instead of wasting 10 pointless retries.
2. **Fixed app/DuckDB startup overhead** (`_BASELINE_OVERHEAD_MB = 500`).
3. **Final point cost** (`_BYTES_PER_POINT`, `_SAFETY_MARGIN`) — the
   DISTANCE-dependent `ST_VoronoiDiagram` → join → union sequence, fitted at
   ~1.82 KB/point from 7 concordant real+synthetic data points in-container.

`--memory-gb` (default 4, env `MEMORY_GB`) parameterizes this instead of
assuming one fixed ceiling fits every deployment — the same formula
re-derives a safe budget for a smaller container or, eventually, a WASM heap
limit. Verified in the real 4GB container after the fix: Chad/Algeria
unaffected, **Chile now succeeds** (previously OOM'd even at the old
"calibrated" default).

**Not yet fixed — two separate, pre-existing bottlenecks found during this
verification, out of scope for the DISTANCE-budget fix above:**
- `idn_admin3` OOMs in `_02_lines.py`'s neighbor-union bbox self-join
  (dies after `_02_tmp1`, before `_02_tmp2` completes) — a pre-existing
  pattern this project has separately flagged before as capable of blowing
  up on high-feature-count files, apparently never stress-tested under a
  real hard memory ceiling until now.
- `phl_admin3` OOMs even earlier, in `_01_inputs.py`'s `ST_CoverageClean`
  pass (triggered by invalid-edge detection in the source data) — fails
  before `lines` or `attempt.py` ever run.

Both were likely masked on a dev machine by abundant RAM+swap rather than
actually being safe. Needs separate investigation.

The rest of this document is the original investigation history, kept for
context on how the root cause was diagnosed.

## Context
Ran the full 147-file `inputs/` corpus through the DuckDB pipeline (duckdb-migration
branch). Found large per-file duration outliers unrelated to file size. This doc
picks up mid-investigation into *why*.

## Already fixed and committed (not part of this investigation)
Commit `4047ae7` on `duckdb-migration`: `app/__main__.py` now isolates each file in
`input_dir` into its own subprocess (`_run_isolated`) instead of looping in one
process — fixes unbounded RSS growth (~2GB → ~19.85GB observed) across a multi-file
batch. Documented in `CLAUDE.md` under "Batch Processing Many Files". This is done;
don't re-investigate memory accumulation, that thread is closed.

## Outlier durations from the full clean run (subprocess-isolated, trustworthy)
| file | duration | features | exterior boundary (deg) |
|---|---|---|---|
| dza_admin2 (Algeria) | ~24min (from contaminated single-process era, NOT re-verified clean) | 1,541 | 81.2 |
| tcd_admin2 (Chad) | 1115s / ~18.6min (CLEAN, isolated, verified) | 70 | 52.9 |
| mli_admin3_v01/v02 (Mali) | 729s/814s | 701 | 71.8 |
| sau_admin1 (Saudi Arabia) | 791s | 13 | 128.8 |
| ner_admin2/v03 (Niger) | 551s/481s | 63/266 | 52.6/52.8 |
| lby_admin2 (Libya) | 481s | 22 | 59.3 |
| chl_admin3 (Chile, the "canonical" stress test) | 325s (comparatively FAST) | 345 | 1106.8 |
| idn_admin3 (Indonesia) | 459s (comparatively FAST) | 7,069 | 910.3 |
| are_admin1 (fast baseline) | 53s / 54s clean-reconfirmed | 7 | 42.3 |
| bdi_admin2 (fast baseline) | 4s clean-reconfirmed | 119 | 10.5 |

## Hypotheses tested and FALSIFIED (don't re-test these)
1. **Coastline/international-border length drives duration.** Falsified: Chile
   (1106.8° exterior boundary, the largest in the set) and Indonesia (910.3°) are
   both comparatively FAST. The slowest files (Chad, Mali, Niger, Algeria — several
   landlocked) have small exterior boundaries (52-82°), same range as the fast
   baseline files.
2. **Raw total boundary length (interior + exterior) drives duration.** Partially
   correlates but confounded: `col_admin3` and `idn_admin3` have the largest raw
   boundary lengths (6538°, 5129°) yet aren't the slowest, because their predicted
   point count (`boundary_len / DISTANCE`, DISTANCE=0.0002) exceeds
   `MAX_POINTS=10,000,000` in `app/config.py`, which almost certainly trips
   `attempt.py`'s doubling-distance retry and reprocesses them at a coarser,
   cheaper spacing. Files landing just BELOW that cap (dza_admin2, chl_admin3,
   eth_admin3_v03, ~7.7-8.5M predicted points) run at full density with no
   mitigation.
3. **Bbox-overlap false-positives in `_02_lines.py`'s neighbor-union self-join
   cause the slowdown** (hypothesis: sprawling admin polygons in Chad have
   overlapping bboxes despite not truly touching, bloating `ST_Union_Agg` /
   `ST_Difference` cost). Falsified directly for Chad: profiled with `--debug`,
   the `_02_tmp2` neighbor-union query (the exact bbox self-join in question) ran
   in **0.022s at 83MB** — trivially cheap, not the bottleneck.

## The actual finding (confirmed for Chad, NOT yet confirmed for others)
Profiled `tcd_admin2` with `--debug` in a clean isolated process. The cost is
concentrated entirely in `_04_tmp1` (`app/_04_voronoi.py`'s `ST_VoronoiDiagram`
call): **296.349 seconds at only 636MB RSS peak.**

This is the key signature: LOW memory + HIGH time. Every other slow file profiled
so far (Chile, Mexico) showed high time correlating with high memory (multi-GB).
Chad's profile is different — it's not a volume problem (only ~264K predicted
points, tiny compared to Chile's ~5.5M or Algeria's ~8M), it's an **algorithmic
degeneracy** in GEOS's Voronoi diagram computation.

**Working theory (plausible, not yet directly verified):** Chad's admin2
boundaries are likely long, straight desert lines cutting across largely featureless
terrain (common for interior administrative divisions in sparse, arid countries).
The points interpolated along these lines (`_03_points.py`, fixed spacing) would be
heavily collinear. Collinear/near-degenerate point configurations are a known
pathological case for Voronoi-diagram algorithms (can degrade toward worse-than-
O(n log n) behavior), independent of point count or memory.

The user (informed guess, not verified) believes `dza_admin2` (Algeria — also a
large, sparse, desert country with a similar administrative-boundary profile) shares
this same mechanism. Plausible but not independently confirmed — Algeria has far
more features (1,541 vs Chad's 70) and a much higher predicted point count, so it's
also possible Algeria's cost is a genuine volume story rather than a pure
degeneracy story. Worth a clean `--debug` profile of `dza_admin2` alone if this
needs to be nailed down rather than assumed.

## Suggested next steps for the Voronoi fix
1. **Verify the collinearity theory concretely** rather than assume it — e.g. check
   point configurations along `tcd_admin2`'s `_03b` table for collinearity/duplicate
   coordinates, or test whether jittering/perturbing points before
   `ST_VoronoiDiagram` avoids the pathology (would confirm root cause but isn't
   necessarily production-safe by itself).
2. **Check DuckDB/GEOS version + `ST_VoronoiDiagram` docs** for any documented
   tolerance/precision parameters that affect degenerate-input handling — per this
   repo's `CLAUDE.md` rule, verify against installed version, don't rely on
   recalled GEOS behavior.
3. Consider whether a small, targeted perturbation (e.g., tiny random jitter on
   interpolated points before Voronoi, or snapping to avoid exact collinearity)
   is an acceptable fix given the project's stated constraint: "Original geometry
   must not be touched" — but note that constraint (per existing memory
   `feedback_original_geometry_untouched`) applies to `_01`/original polygon
   boundaries, NOT to the synthetic interpolated Voronoi-generator points in
   `_03b`, so jittering generator points is likely in-bounds — confirm this
   distinction before implementing.
4. Reproduce the pathology minimally (small synthetic collinear point set) to
   confirm it's really collinearity and not something else about Chad's data
   before investing in a fix.

## How to reproduce the Chad profile
```
uv run -m app --input-file=tcd_admin2.parquet \
  --output-dir=<scratch>/clean_out --tmp-dir=<scratch>/clean_tmp \
  --debug --overwrite
```
Watch for the `_04_tmp1` query line in the log — that's the `ST_VoronoiDiagram` call.

## Repo state
Branch `duckdb-migration`, HEAD at commit `4047ae7` ("isolate multi-file batch runs
into per-file subprocesses"), 4 commits ahead of `origin/duckdb-migration`, not yet
pushed. Full 147-file batch output sits in `outputs/` (gitignored, not committed).
