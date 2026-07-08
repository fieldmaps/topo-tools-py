"""Non-user-configurable constants for the clean pipeline."""

# Default detection cutoff for ST_CoverageInvalidEdges_Agg's tolerance
# parameter, in meters. Disabled (0) by default -- _build_slivers's
# gap/overlap-subtraction step (buffer + cross join + ST_Difference against
# unioned blobs) has triggered the DuckDB SPATIAL_JOIN-class memory
# reservation OOM on tiny real-world inputs (Angola admin1, 21 fids), not
# just at scale. Detect-only with no fix path makes chasing that bug low
# value right now; revisit once DuckDB ships ST_Snap and slivers can
# actually be fixed, not just reported. Opt back in with --sliver-tolerance.
# Was 10.0 (ported from topo-tools-js's SLIVER_TOL_DEFAULT_M) before this.
SLIVER_TOLERANCE_DEFAULT_M = 0.0

# Floor below which detected gap/overlap regions are discarded as
# floating-point noise rather than real defects. Ported from
# topo-tools-js's MIN_ISSUE_AREA_M2 -- observed float-jitter artifacts there
# topped out at 1.6e-7 m^2, so 1e-4 m^2 (1cm^2) leaves generous headroom.
MIN_ISSUE_AREA_M2 = 1e-4

# Precision-reduction retry grid size (degrees) on a GEOS TopologyException
# from any detection/clean query. Ported from topo-tools-js's
# REDUCED_PRECISION_DEG -- empirically the largest value giving a stable,
# reproducible result (1e-11/1e-12 "succeeded" but were numerically
# unstable, producing a different result each run).
REDUCED_PRECISION_DEG = 1e-10

# Relative headroom added to the widest detected gap's own width when
# --gap-width=all resolves gap_maximum_width, so the widest gap itself
# reliably clears ST_CoverageClean's <= comparison rather than landing
# exactly on the boundary.
ALL_GAP_WIDTH_EPSILON_FACTOR = 1.001
