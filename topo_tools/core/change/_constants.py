"""Non-user-configurable constants for the change pipeline."""

# Minimum max(coverage_a, coverage_b) for two units to be spatially linked
# (union-find edge). Ported from topo-tools-js's current default (bumped in
# commit 420f2ad).
TAU_MATCH_DEFAULT = 0.8

# Minimum IoU for a 1:1 spatially-linked pair to be unchanged/renamed rather
# than modified. Ported from topo-tools-js's current App.svelte default.
TAU_SAME_DEFAULT = 0.98

# Equal Earth -- used to compute shared/whole-polygon areas for coverage_a/
# coverage_b/iou ratios (never stored). Duplicated from core/match/_constants.py's
# EQUAL_AREA_CRS rather than imported: change stays decoupled from match/
# clean the same way they're decoupled from each other.
EQUAL_AREA_CRS = "EPSG:8857"

# Intersection crumbs below this area (deg^2, ~1cm^2) are dropped as noise
# before shared-area aggregation. Ported as-is from topo-tools-js's
# src/lib/db/overlap.ts SLIVER constant.
INTERSECTION_SLIVER_DEG2 = 1e-12

# Tabular changelog export has no geometry column, so extend's GDAL-vector
# COPY_OPTS doesn't apply -- FORMAT CSV/PARQUET need no spatial extension.
TABLE_COPY_OPTS = {
    ".csv": "(FORMAT CSV, HEADER)",
    ".parquet": "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15)",
}
