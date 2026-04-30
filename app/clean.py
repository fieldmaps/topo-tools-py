"""DuckDB-native ST_CoverageClean (v2) — surgical, per-cluster.

The previous attempt (clean-coverage-archive) globally polygonized every
polygon's ST_Boundary, which OOM-killed on `col_admin3` because the atom
count from ST_Polygonize over the entire country's boundaries blows up
when the input has many overlaps. This rewrite scopes the polygonize-and-
reattribute pass to *connected components* of residual bad edges so memory
scales with the largest dirty cluster, not the whole country.

Pipeline:

1. Detect bad edges via ST_CoverageInvalidEdges_Agg. If the coverage is
   already valid, exit early (the only cost paid for clean inputs).

2. Buffer the bad-edge multilinestring by a small distance (1e-5°) so
   adjacent segments merge into spatial clusters via ST_Buffer's natural
   union. Each connected piece is one cluster.

3. Per cluster, identify the involved polygons in two passes:
   a. CORE: polygons whose geometry intersects the cluster (their boundary
      contains a bad edge).
   b. NEIGHBOURS: polygons that touch any core polygon. Without these,
      Phase C produces new bad edges between cleaned core polygons and
      their unmodified outside-cluster neighbours.

4. Merge clusters that share a polygon into super-clusters via union-find.
   Without this, a polygon appearing in multiple clusters (core in one,
   neighbour in another) gets duplicated atoms across cluster polygonize
   calls and ST_Union_Agg per fid produces sub-pixel-shifted boundaries
   that ST_CoverageInvalidEdges keeps flagging round after round.

5. Per super-cluster, ST_Boundary → ST_Node → ST_Polygonize → assign each
   atom to a fid by point-in-polygon (interior winners by longest border)
   or boundary length (gap atoms / slivers).

6. Rebuild involved polygon geometries via ST_MakeValid(ST_Union_Agg(atom))
   per fid. Non-involved fids keep their original geometry via COALESCE.

7. Iterate steps 1-6 up to _MAX_ROUNDS rounds. Each round shrinks the
   residual by reprocessing edges that fell at the previous round's cluster
   perimeter. Empirically converges in 1-3 rounds.

Memory safety: an explicit `cluster_polygon_cap` skips clusters whose core
or expanded set exceeds the cap, so Phase C never regresses to the failed
global-polygonize OOM. Skipped clusters keep their pre-round geometry.

Phase A (pairwise overlap removal via ST_Difference) was prototyped and
removed: the intermediate ST_Difference geometries produced degenerate
sub-pixel slivers that caused ST_Node in step 5 to fail with "Iterated
noding failed to converge". Step 5's polygonize-and-reattribute handles
overlaps directly (overlap atoms get assigned to the longest-border
polygon).

Swap boundary: when DuckDB-spatial wraps GEOS 3.14's coverage_clean (see
duckdb/duckdb-spatial discussion #679), the body collapses to
`SELECT ST_CoverageClean(list(geom), ...) FROM _01`. Signature mirrors
PostGIS ST_CoverageClean(geom_array, snapping_distance, gap_maximum_width,
overlap_merge_strategy).
"""

from logging import getLogger
from typing import Literal

from duckdb import DuckDBPyConnection

from .config import debug

OverlapStrategy = Literal["largest_area", "merge_longest_border"]

logger = getLogger(__name__)

# Cluster-size cap: skip clusters with more involved polygons than this so
# Phase C never regresses to the failed global-polygonize OOM. 50 covers
# almost any realistic dirty patch (e.g. an admin re-numbering with a few
# overlapping districts). Larger clusters indicate input that is too dirty
# for SQL-based cleanup to handle reliably.
_DEFAULT_CLUSTER_POLYGON_CAP = 50

# Buffer used to merge bad-edge segments into spatial clusters. Must exceed
# the FP drift between mismatched polygon boundaries — bad edges trace one
# side of the gap, so the other polygon's boundary sits ~mismatch-width away
# and is missed if the buffer is too tight. 1e-5 degrees (~1 m) catches
# typical sub-pixel mismatches without over-including unrelated polygons.
_CLUSTER_BUFFER = 1e-5

# Phase C iteration cap — each round shrinks the residual by reprocessing
# edges at the previous round's cluster perimeter. Empirically converges in
# 1-3 rounds; cap prevents infinite loops on pathological inputs.
_MAX_ROUNDS = 5


def main(  # noqa: PLR0913 - mirrors ST_CoverageClean signature
    conn: DuckDBPyConnection,
    name: str,
    *,
    snapping_distance: float = 0.0,  # noqa: ARG001 - reserved for ST_CoverageClean swap
    gap_maximum_width: float = 0.0001,  # noqa: ARG001 - reserved for lake detection
    gap_max_thinness: float = 0.05,  # noqa: ARG001 - reserved for lake detection
    overlap_strategy: OverlapStrategy = "merge_longest_border",  # noqa: ARG001 - currently always longest border
    cluster_polygon_cap: int = _DEFAULT_CLUSTER_POLYGON_CAP,
) -> None:
    """Clean coverage errors in `_01` via two-phase pairwise + surgical pass.

    The signature mirrors PostGIS ST_CoverageClean so the body can be
    swapped for the native call once DuckDB-spatial wraps GEOS 3.14's
    coverage_clean. Currently `gap_*` and `overlap_strategy` are reserved
    parameters — Phase C always uses longest-border for sliver assignment
    (gaps live between polygons, so largest-area is meaningless), and
    lake-vs-sliver detection is skipped because clusters only form around
    real bad edges (legitimate lake interior rings are not flagged by
    ST_CoverageInvalidEdges_Agg in a valid coverage).
    """
    if not _has_coverage_errors(conn, name):
        return
    logger.info("clean: coverage errors detected in %s_01", name)

    # NOTE Phase A (pairwise overlap removal via ST_Difference) is intentionally
    # disabled here. The intermediate ST_Difference geometries produced
    # degenerate sub-pixel slivers that caused ST_Node in Phase C to fail with
    # "Iterated noding failed to converge". The surgical Phase C alone bounds
    # memory by cluster size and handles overlaps directly via the
    # polygonize-and-reattribute primitive (overlap atoms get assigned to the
    # longest-border polygon, same outcome as Phase A's ST_Difference but
    # without the intermediate-geometry bugs).

    # Phase C: surgical polygonize on connected components of residual bad
    # edges. Iterates until convergence or _MAX_ROUNDS — each round shrinks
    # the residual by reprocessing edges that fell at the previous round's
    # cluster perimeter (where cleaned core boundaries no longer matched
    # un-cleaned outside-cluster polygons). Three rounds resolves cod_admin3
    # in practice; bigger residuals would tail off via the same mechanism.
    for round_num in range(1, _MAX_ROUNDS + 1):
        n_clusters, n_skipped = _phase_c_surgical_clean(conn, name, cluster_polygon_cap)
        logger.info(
            "clean: phase C round %d — %d cluster(s) processed, %d oversized skipped",
            round_num,
            n_clusters,
            n_skipped,
        )
        if not _has_coverage_errors(conn, name):
            break
    else:
        logger.warning(
            "clean: residual coverage errors remain in %s_01 after %d rounds; "
            "downstream topology checks will decide pass/fail",
            name,
            _MAX_ROUNDS,
        )

    if not debug:
        _drop_tmp_tables(conn, name)


def _has_coverage_errors(conn: DuckDBPyConnection, name: str) -> bool:
    """Return True if ST_CoverageInvalidEdges_Agg flags any bad edges."""
    return conn.execute(f"""--sql
        WITH agg AS (
            SELECT ST_CoverageInvalidEdges_Agg(geom) AS g FROM "{name}_01"
        )
        SELECT g IS NOT NULL AND NOT ST_IsEmpty(g) FROM agg
    """).fetchall()[0][0]


def _phase_c_surgical_clean(
    conn: DuckDBPyConnection, name: str, cluster_polygon_cap: int
) -> tuple[int, int]:
    """Polygonize-and-reattribute, scoped to connected dirty clusters.

    Returns (clusters_processed, clusters_skipped). Skipped clusters
    exceed `cluster_polygon_cap` to prevent regression to the failed
    global-polygonize OOM.
    """
    # 1. Materialize bad edges (we know they exist — caller checked).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp2" AS
        SELECT ST_CoverageInvalidEdges_Agg(geom) AS bad_edges
        FROM "{name}_01"
    """)

    # 2. Cluster bad edges spatially. ST_Buffer on a multilinestring returns
    # a (multi)polygon where overlapping buffers are already merged by GEOS
    # — no separate union step needed. ST_Dump gives one row per connected
    # cluster. Buffer is small relative to typical polygon sizes but big
    # enough to absorb FP drift between connected segments.
    # ROW_NUMBER must be applied AFTER UNNEST — when window functions sit on
    # the same SELECT as UNNEST, DuckDB evaluates them at the source-table
    # level, so every UNNEST'd part inherits the same row number. Splitting
    # into a separate `dumped` CTE materialises the per-cluster rows first.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp3" AS
        WITH buffered AS (
            SELECT ST_Buffer(bad_edges, {_CLUSTER_BUFFER!r}) AS bg
            FROM "{name}_01_clean_tmp2"
        ),
        dumped AS (
            SELECT UNNEST(ST_Dump(bg)).geom AS cluster_geom
            FROM buffered
        )
        SELECT ROW_NUMBER() OVER () AS cid, cluster_geom FROM dumped
    """)

    # 3a. Core polygons: those whose geometry intersects a cluster_geom.
    # Their boundaries contain the bad edges.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp4" AS
        SELECT DISTINCT c.cid, p.fid, p.geom
        FROM "{name}_01_clean_tmp3" c
        JOIN "{name}_01" p
          ON ST_XMax(c.cluster_geom) >= ST_XMin(p.geom)
         AND ST_XMin(c.cluster_geom) <= ST_XMax(p.geom)
         AND ST_YMax(c.cluster_geom) >= ST_YMin(p.geom)
         AND ST_YMin(c.cluster_geom) <= ST_YMax(p.geom)
         AND ST_Intersects(c.cluster_geom, p.geom)
    """)

    # 3b. Skip clusters whose CORE alone exceeds the cap before paying the
    # expensive neighbour-expansion join. A cluster that already has 60+
    # core polygons will only grow under expansion; computing neighbours
    # for it is wasted work. On col_admin3, this saved ~13 minutes.
    n_skipped_pre_expand = _drop_oversized_clusters(
        conn, name, cluster_polygon_cap, where="core"
    )

    # 3c. Neighbour expansion: polygons that touch any core polygon.
    # Without these, Phase C produces new bad edges between cleaned core
    # polygons and their unmodified outside-cluster neighbours, because the
    # polygonize input would lack the neighbour's boundary line. Adding
    # neighbours' boundaries ensures the cluster's outer perimeter is
    # preserved vertex-for-vertex (atoms there come from the neighbour's
    # own boundary, and ST_Union_Agg per fid rebuilds the neighbour from
    # those atoms — same input, same output).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp4" AS
        WITH core AS (SELECT * FROM "{name}_01_clean_tmp4"),
        neighbours AS (
            SELECT DISTINCT c.cid, q.fid, q.geom
            FROM core c
            JOIN "{name}_01" q
              ON q.fid != c.fid
             AND ST_XMax(c.geom) >= ST_XMin(q.geom)
             AND ST_XMin(c.geom) <= ST_XMax(q.geom)
             AND ST_YMax(c.geom) >= ST_YMin(q.geom)
             AND ST_YMin(c.geom) <= ST_YMax(q.geom)
             AND ST_Intersects(c.geom, q.geom)
        )
        SELECT cid, fid, ANY_VALUE(geom) AS geom
        FROM (SELECT * FROM core UNION ALL SELECT * FROM neighbours)
        GROUP BY cid, fid
    """)

    # Merge clusters that share a polygon into super-clusters. Without this,
    # a polygon appearing in multiple clusters (core in one, neighbour in
    # another) gets atoms from both polygonize calls — they overlap and
    # ST_Union_Agg per fid produces sub-pixel-shifted boundaries that
    # ST_CoverageInvalidEdges keeps flagging. Union-find on (cid, fid)
    # adjacency gives each fid exactly one super-cluster.
    _merge_clusters_with_shared_fids(conn, name)

    # Skip oversized clusters again after neighbour expansion (some clusters
    # under the cap on core may exceed it once neighbours are added).
    n_skipped_post_expand = _drop_oversized_clusters(
        conn, name, cluster_polygon_cap, where="expanded"
    )
    n_skipped_total = n_skipped_pre_expand + n_skipped_post_expand

    n_clusters = conn.execute(
        f'SELECT COUNT(DISTINCT cid) FROM "{name}_01_clean_tmp4"'
    ).fetchall()[0][0]
    if n_clusters == 0:
        return 0, n_skipped_total

    # 4. Local polygonize PER CLUSTER. The GROUP BY cid scopes ST_Node and
    # ST_Polygonize to one cluster's lines at a time — DuckDB still
    # parallelizes across clusters, but each call's working memory is
    # bounded by cluster size, not country size.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp5" AS
        WITH local_lines AS (
            SELECT cid, ST_Boundary(geom) AS line
            FROM "{name}_01_clean_tmp4"
        ),
        per_cluster_noded AS (
            SELECT cid, ST_Node(ST_Collect(list(line))) AS noded
            FROM local_lines
            GROUP BY cid
        ),
        per_cluster_atoms AS (
            SELECT cid, UNNEST(ST_Dump(ST_Polygonize([noded]))).geom AS atom
            FROM per_cluster_noded
        )
        SELECT cid, ROW_NUMBER() OVER () AS aid, atom
        FROM per_cluster_atoms
    """)

    # 5. One representative interior point per involved-polygon part. Used
    # by atom assignment to identify which polygons are candidates for each
    # atom (point-in-polygon).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp6" AS
        WITH parts AS (
            SELECT cid, fid, UNNEST(ST_Dump(geom)).geom AS part_geom
            FROM "{name}_01_clean_tmp4"
        )
        SELECT cid, fid, ST_PointOnSurface(part_geom) AS pt
        FROM parts
    """)

    # 6. Assign each atom to a fid. Mirrors the archive's _01_tmp4 logic
    # (interior winners by strategy metric; gap atoms by longest border)
    # but partitioned per-cluster so the joins stay local.
    #
    # Always uses longest-border for both interior and gap assignment in
    # this v2 — keeps the SQL simple and matches PostGIS default. The
    # `largest_area` strategy can be wired back in later if needed.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp7" AS
        WITH candidates AS (
            SELECT DISTINCT a.cid, a.aid, a.atom, pt.fid,
                   ST_Length(ST_Intersection(
                       ST_Boundary(a.atom), ST_Boundary(p.geom)
                   )) AS metric
            FROM "{name}_01_clean_tmp5" a
            JOIN "{name}_01_clean_tmp6" pt
              ON pt.cid = a.cid
             AND ST_X(pt.pt) >= ST_XMin(a.atom)
             AND ST_X(pt.pt) <= ST_XMax(a.atom)
             AND ST_Y(pt.pt) >= ST_YMin(a.atom)
             AND ST_Y(pt.pt) <= ST_YMax(a.atom)
             AND ST_Within(pt.pt, a.atom)
            JOIN "{name}_01_clean_tmp4" p
              ON p.cid = a.cid AND p.fid = pt.fid
        ),
        interior_winners AS (
            SELECT cid, aid, atom, fid
            FROM candidates
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY cid, aid ORDER BY metric DESC, fid ASC
            ) = 1
        ),
        gap_atoms AS (
            SELECT a.cid, a.aid, a.atom
            FROM "{name}_01_clean_tmp5" a
            WHERE NOT EXISTS (
                SELECT 1 FROM "{name}_01_clean_tmp6" pt
                WHERE pt.cid = a.cid AND ST_Within(pt.pt, a.atom)
            )
        ),
        sliver_assigned AS (
            SELECT g.cid, g.aid, g.atom, p.fid
            FROM gap_atoms g
            JOIN "{name}_01_clean_tmp4" p
              ON p.cid = g.cid
             AND ST_XMax(g.atom) >= ST_XMin(p.geom)
             AND ST_XMin(g.atom) <= ST_XMax(p.geom)
             AND ST_YMax(g.atom) >= ST_YMin(p.geom)
             AND ST_YMin(g.atom) <= ST_YMax(p.geom)
             AND ST_Intersects(ST_Boundary(g.atom), ST_Boundary(p.geom))
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY g.cid, g.aid
                ORDER BY ST_Length(ST_Intersection(
                    ST_Boundary(g.atom), ST_Boundary(p.geom)
                )) DESC, p.fid ASC
            ) = 1
        )
        SELECT cid, aid, atom, fid FROM interior_winners
        UNION ALL
        SELECT cid, aid, atom, fid FROM sliver_assigned
    """)

    # 7. Rebuild only the involved fids. ST_Union_Agg per fid stitches
    # cleaned atoms back into one (multi)polygon; non-involved fids keep
    # their existing geometry via COALESCE. Atoms assigned to fids in
    # skipped (oversized) clusters are not present in tmp7, so those fids
    # also fall through to COALESCE — they keep their pre-Phase-C state
    # rather than being rebuilt from a partial atom set.
    # ST_MakeValid on the rebuilt geometry repairs any tiny self-intersection
    # produced by ST_Union_Agg's FP-sensitive boundary stitching. Without it,
    # downstream ST_VoronoiDiagram has crashed with SIGSEGV on rebuilt
    # cod_admin3 polygons whose ST_Union_Agg output had hairline self-touches.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01" AS
        WITH per_fid AS (
            SELECT fid, ST_MakeValid(ST_Union_Agg(atom)) AS new_geom
            FROM "{name}_01_clean_tmp7"
            GROUP BY fid
        )
        SELECT p.* EXCLUDE (geom),
               COALESCE(pf.new_geom, p.geom) AS geom
        FROM "{name}_01" p
        LEFT JOIN per_fid pf ON p.fid = pf.fid
    """)

    return n_clusters, n_skipped_total


def _drop_oversized_clusters(
    conn: DuckDBPyConnection, name: str, cap: int, *, where: str
) -> int:
    """Delete clusters whose tmp4 row count exceeds `cap`. Returns count dropped.

    Called twice in Phase C: once on cores only (saves the expensive
    neighbour join for clusters that will be skipped anyway), and once
    after expansion (catches clusters that grow over the cap from
    neighbour-padding). The `where` arg only affects the log message.
    """
    sizes = conn.execute(f"""--sql
        SELECT cid, COUNT(*) AS nfids
        FROM "{name}_01_clean_tmp4"
        GROUP BY cid
        ORDER BY nfids DESC
    """).fetchall()
    oversized = [(cid, n) for cid, n in sizes if n > cap]
    if not oversized:
        return 0
    excluded = ",".join(str(cid) for cid, _ in oversized)
    logger.warning(
        "clean: skipping %d oversized cluster(s) at %s (cap=%d, largest=%d polygons)",
        len(oversized),
        where,
        cap,
        oversized[0][1],
    )
    conn.execute(f'DELETE FROM "{name}_01_clean_tmp3" WHERE cid IN ({excluded})')
    conn.execute(f'DELETE FROM "{name}_01_clean_tmp4" WHERE cid IN ({excluded})')
    return len(oversized)


def _merge_clusters_with_shared_fids(conn: DuckDBPyConnection, name: str) -> None:
    """Rewrite tmp4.cid so clusters sharing a polygon collapse to one super-cluster.

    A union-find over (cid, fid) adjacency: any two cids that share a fid
    must merge. The resulting cid is the smallest member of the connected
    component. Done in Python because DuckDB has no native union-find and a
    recursive CTE for transitive closure is slow on hundreds of clusters.
    """
    rows = conn.execute(f'SELECT cid, fid FROM "{name}_01_clean_tmp4"').fetchall()
    if not rows:
        return

    fid_to_cids: dict[int, list[int]] = {}
    for cid, fid in rows:
        fid_to_cids.setdefault(fid, []).append(cid)

    parent: dict[int, int] = {cid: cid for cid, _ in rows}

    def find(c: int) -> int:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for cids in fid_to_cids.values():
        if len(cids) <= 1:
            continue
        first = cids[0]
        for c in cids[1:]:
            ra, rb = find(first), find(c)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

    cid_to_root = {cid: find(cid) for cid in parent}
    if all(k == v for k, v in cid_to_root.items()):
        return  # No merges needed.

    mapping_values = ", ".join(f"({old}, {new})" for old, new in cid_to_root.items())
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01_clean_tmp4" AS
        SELECT m.new_cid AS cid, t.fid, ANY_VALUE(t.geom) AS geom
        FROM "{name}_01_clean_tmp4" t
        JOIN (VALUES {mapping_values}) m(old_cid, new_cid)
          ON t.cid = m.old_cid
        GROUP BY m.new_cid, t.fid
    """)


def _drop_tmp_tables(conn: DuckDBPyConnection, name: str) -> None:
    for n in range(2, 8):
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01_clean_tmp{n}"')
