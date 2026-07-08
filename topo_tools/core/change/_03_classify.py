"""Classifies old/new polygon pairs into relationship classes.

Ported from topo-tools-js's polygon-changelog/pipeline/classify.ts, run in
Python rather than SQL -- union-find and cardinality classification scale
with feature count, not vertex count, so an in-memory pass is safe under
this repo's memory model even for a large admin layer.
"""

from dataclasses import dataclass
from logging import getLogger

from duckdb import DuckDBPyConnection

from ._unionfind import UnionFind

logger = getLogger(__name__)


@dataclass
class _Pair:
    a_fid: int
    b_fid: int
    shared_area: float | None
    coverage_a: float | None
    coverage_b: float | None
    iou: float | None
    a_code: str | None
    a_name: str | None
    b_code: str | None
    b_name: str | None
    rescued_by_identity: bool = False


def _unique_values(rows: list[tuple], col_idx: int) -> set[str]:
    """Values appearing exactly once in rows[:, col_idx] -- excludes duplicates.

    Duplicate values (e.g. a shared "No_Pcode" placeholder) can't reliably
    identify a single unit; matching on them would union every polygon
    sharing that value into one cluster.
    """
    counts: dict[str, int] = {}
    for row in rows:
        v = row[col_idx]
        if v is not None:
            counts[v] = counts.get(v, 0) + 1
    return {v for v, n in counts.items() if n == 1}


def _identity_match(  # noqa: PLR0913 -- each param is a distinct required input, not decomposable
    pair: _Pair,
    *,
    link_by_code: bool,
    link_by_name: bool,
    link_mode: str,
    unique_codes_a: set[str],
    unique_codes_b: set[str],
    unique_names_a: set[str],
    unique_names_b: set[str],
) -> bool:
    """Return True if pair's code and/or name match across versions, per link settings.

    A value only qualifies when it is unique on each side. NULL-safe: a NULL
    code/name on either side never counts as a match.
    """
    if not link_by_code and not link_by_name:
        return False
    code_match = (
        link_by_code
        and pair.a_code is not None
        and pair.a_code == pair.b_code
        and pair.a_code in unique_codes_a
        and pair.b_code in unique_codes_b
    )
    name_match = (
        link_by_name
        and pair.a_name is not None
        and pair.a_name == pair.b_name
        and pair.a_name in unique_names_a
        and pair.b_name in unique_names_b
    )
    if link_by_code and link_by_name:
        return (
            (code_match and name_match)
            if link_mode == "both"
            else (code_match or name_match)
        )
    return code_match or name_match


def main(  # noqa: C901, PLR0912, PLR0913, PLR0915 -- ported classification algorithm, not decomposable without losing the single coherent pass
    conn: DuckDBPyConnection,
    name: str,
    *,
    tau_match: float,
    tau_same: float,
    link_by_code: bool,
    link_by_name: bool,
    link_mode: str,
    code_col_a: str | None,
    code_col_b: str | None,
    name_col_a: str | None,
    name_col_b: str | None,
) -> None:
    """Classify every old/new fid pair, writing {name}_03a/{name}_03b/{name}_03c."""
    code_expr_a = f'CAST("{code_col_a}" AS VARCHAR)' if code_col_a else "NULL::VARCHAR"
    code_expr_b = f'CAST("{code_col_b}" AS VARCHAR)' if code_col_b else "NULL::VARCHAR"
    name_expr_a = f'CAST("{name_col_a}" AS VARCHAR)' if name_col_a else "NULL::VARCHAR"
    name_expr_b = f'CAST("{name_col_b}" AS VARCHAR)' if name_col_b else "NULL::VARCHAR"

    a_rows = conn.execute(f"""--sql
        SELECT fid, {code_expr_a} AS code, {name_expr_a} AS name FROM "{name}_a_01"
    """).fetchall()
    b_rows = conn.execute(f"""--sql
        SELECT fid, {code_expr_b} AS code, {name_expr_b} AS name FROM "{name}_b_01"
    """).fetchall()

    a_code_by_fid = {r[0]: r[1] for r in a_rows}
    a_name_by_fid = {r[0]: r[2] for r in a_rows}
    b_code_by_fid = {r[0]: r[1] for r in b_rows}
    b_name_by_fid = {r[0]: r[2] for r in b_rows}

    unique_codes_a = _unique_values(a_rows, 1)
    unique_codes_b = _unique_values(b_rows, 1)
    unique_names_a = _unique_values(a_rows, 2)
    unique_names_b = _unique_values(b_rows, 2)

    pair_rows = conn.execute(f"""--sql
        SELECT a_fid, b_fid, shared_area, coverage_a, coverage_b, iou FROM "{name}_02"
    """).fetchall()

    pairs = [
        _Pair(
            a_fid=r[0],
            b_fid=r[1],
            shared_area=r[2],
            coverage_a=r[3],
            coverage_b=r[4],
            iou=r[5],
            a_code=a_code_by_fid.get(r[0]),
            a_name=a_name_by_fid.get(r[0]),
            b_code=b_code_by_fid.get(r[1]),
            b_name=b_name_by_fid.get(r[1]),
        )
        for r in pair_rows
    ]

    uf = UnionFind()
    for fid in a_code_by_fid:
        uf.add(f"a:{fid}")
    for fid in b_code_by_fid:
        uf.add(f"b:{fid}")

    identity_kwargs = {
        "link_by_code": link_by_code,
        "link_by_name": link_by_name,
        "link_mode": link_mode,
        "unique_codes_a": unique_codes_a,
        "unique_codes_b": unique_codes_b,
        "unique_names_a": unique_names_a,
        "unique_names_b": unique_names_b,
    }

    # Which B fids each A fid reaches via tau_match (and vice versa) -- used
    # by the identity claim guard below.
    spatial_neighbors_a: dict[int, set[int]] = {}
    spatial_neighbors_b: dict[int, set[int]] = {}
    for p in pairs:
        if p.coverage_a is None or p.coverage_b is None:
            continue
        if max(p.coverage_a, p.coverage_b) < tau_match:
            continue
        spatial_neighbors_a.setdefault(p.a_fid, set()).add(p.b_fid)
        spatial_neighbors_b.setdefault(p.b_fid, set()).add(p.a_fid)

    identity_a_set: set[int] = set()
    identity_b_set: set[int] = set()
    if link_by_code or link_by_name:
        for p in pairs:
            if _identity_match(p, **identity_kwargs):
                identity_a_set.add(p.a_fid)
                identity_b_set.add(p.b_fid)

    claimed_a: set[int] = set()
    claimed_b: set[int] = set()
    passing_pairs: list[_Pair] = []

    # Phase 1 -- identity: claim a pair ahead of spatial matching only if
    # every other spatial tau_match neighbor of both fids is also
    # identity-covered. Otherwise a genuine split (A -> B1 keeps the code, B2
    # is new) would be incorrectly collapsed into a false 1:1 identity match
    # instead of falling through to Phase 2's spatial clustering.
    if link_by_code or link_by_name:
        for p in pairs:
            if not _identity_match(p, **identity_kwargs):
                continue
            if p.a_fid in claimed_a or p.b_fid in claimed_b:
                continue
            a_neighbors = spatial_neighbors_a.get(p.a_fid, set())
            b_neighbors = spatial_neighbors_b.get(p.b_fid, set())
            all_a_covered = all(
                b == p.b_fid or b in identity_b_set for b in a_neighbors
            )
            all_b_covered = all(
                a == p.a_fid or a in identity_a_set for a in b_neighbors
            )
            if not (all_a_covered and all_b_covered):
                continue
            claimed_a.add(p.a_fid)
            claimed_b.add(p.b_fid)
            spatial_also_pass = (
                p.coverage_a is not None
                and p.coverage_b is not None
                and max(p.coverage_a, p.coverage_b) >= tau_match
            )
            p.rescued_by_identity = not spatial_also_pass
            uf.union(f"a:{p.a_fid}", f"b:{p.b_fid}")
            passing_pairs.append(p)

    # Phase 2 -- spatial: union unclaimed fids whose coverage passes tau_match.
    for p in pairs:
        if p.coverage_a is None or p.coverage_b is None:
            continue
        if max(p.coverage_a, p.coverage_b) < tau_match:
            continue
        if p.a_fid in claimed_a or p.b_fid in claimed_b:
            continue
        p.rescued_by_identity = False
        uf.union(f"a:{p.a_fid}", f"b:{p.b_fid}")
        passing_pairs.append(p)

    components = uf.components()
    cluster_id_by_root = {root: i + 1 for i, root in enumerate(components)}

    cluster_members: dict[int, tuple[list[int], list[int]]] = {}
    for root, members in components.items():
        cid = cluster_id_by_root[root]
        a_fids: list[int] = []
        b_fids: list[int] = []
        for m in members:
            side, fid_str = m.split(":", 1)
            (a_fids if side == "a" else b_fids).append(int(fid_str))
        cluster_members[cid] = (a_fids, b_fids)

    cluster_best_iou: dict[int, float] = {}
    cluster_rescued_only: dict[int, bool] = {}
    cluster_has_attr_change: dict[int, bool] = {}
    for p in passing_pairs:
        cid = cluster_id_by_root[uf.find(f"a:{p.a_fid}")]
        if p.iou is not None:
            cluster_best_iou[cid] = max(cluster_best_iou.get(cid, float("-inf")), p.iou)
        cluster_rescued_only[cid] = (
            cluster_rescued_only.get(cid, True) and p.rescued_by_identity
        )
        code_changed = (
            link_by_code
            and p.a_code is not None
            and p.b_code is not None
            and p.a_code != p.b_code
        )
        name_changed = (
            link_by_name
            and p.a_name is not None
            and p.b_name is not None
            and p.a_name != p.b_name
        )
        cluster_has_attr_change[cid] = (
            cluster_has_attr_change.get(cid, False) or code_changed or name_changed
        )

    cluster_class: dict[int, str] = {}
    for cid, (a_fids, b_fids) in cluster_members.items():
        na, nb = len(a_fids), len(b_fids)
        if na == 1 and nb == 1:
            if cluster_rescued_only.get(cid, False):
                cls = "relocated"
            else:
                iou = cluster_best_iou.get(cid, 0.0)
                if iou >= tau_same:
                    cls = (
                        "renamed"
                        if cluster_has_attr_change.get(cid, False)
                        else "unchanged"
                    )
                else:
                    cls = "modified"
        elif na == 1 and nb > 1:
            cls = "split"
        elif na > 1 and nb == 1:
            cls = "merge"
        elif na > 1 and nb > 1:
            cls = "complex"
        elif na == 1 and nb == 0:
            cls = "removed"
        elif na == 0 and nb == 1:
            cls = "created"
        else:  # pragma: no cover -- unreachable for a connected component
            cls = "complex"
        cluster_class[cid] = cls

    pairs_out: list[tuple] = []
    poly_rows: list[tuple[str, int, int, str]] = []
    changelog_rows: list[tuple] = []

    for p in passing_pairs:
        cid = cluster_id_by_root[uf.find(f"a:{p.a_fid}")]
        cls = cluster_class[cid]
        method = "identity" if p.rescued_by_identity else "spatial"
        pairs_out.append(
            (
                p.a_fid,
                p.b_fid,
                p.shared_area,
                p.coverage_a,
                p.coverage_b,
                p.iou,
                cid,
                cls,
                method,
            )
        )
        changelog_rows.append(
            (
                p.a_code,
                p.a_name,
                p.b_code,
                p.b_name,
                cls,
                method,
                round(p.coverage_a, 3) if p.coverage_a is not None else None,
                round(p.coverage_b, 3) if p.coverage_b is not None else None,
                round(p.iou, 3) if p.iou is not None else None,
                tau_match,
                tau_same,
                link_by_code,
                link_by_name,
                link_mode,
            )
        )

    for cid, (a_fids, b_fids) in cluster_members.items():
        cls = cluster_class[cid]
        poly_rows.extend(("a", fid, cid, cls) for fid in a_fids)
        poly_rows.extend(("b", fid, cid, cls) for fid in b_fids)
        if len(b_fids) == 0:
            changelog_rows.extend(
                (
                    a_code_by_fid.get(fid),
                    a_name_by_fid.get(fid),
                    None,
                    None,
                    cls,
                    None,
                    None,
                    None,
                    None,
                    tau_match,
                    tau_same,
                    link_by_code,
                    link_by_name,
                    link_mode,
                )
                for fid in a_fids
            )
        elif len(a_fids) == 0:
            changelog_rows.extend(
                (
                    None,
                    None,
                    b_code_by_fid.get(fid),
                    b_name_by_fid.get(fid),
                    cls,
                    None,
                    None,
                    None,
                    None,
                    tau_match,
                    tau_same,
                    link_by_code,
                    link_by_name,
                    link_mode,
                )
                for fid in b_fids
            )

    changelog_rows.sort(
        key=lambda r: (r[4], r[0] is None, r[0] or "", r[2] is None, r[2] or "")
    )

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03a" (
            a_fid BIGINT, b_fid BIGINT, shared_area DOUBLE,
            coverage_a DOUBLE, coverage_b DOUBLE, iou DOUBLE,
            cluster_id INTEGER, relationship_class VARCHAR, match_method VARCHAR
        )
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03b" (
            side VARCHAR, fid BIGINT, cluster_id INTEGER, relationship_class VARCHAR
        )
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03c" (
            code_a VARCHAR, name_a VARCHAR, code_b VARCHAR, name_b VARCHAR,
            relationship_class VARCHAR, match_method VARCHAR,
            a_in_b DOUBLE, b_in_a DOUBLE, similarity DOUBLE,
            threshold_match DOUBLE, threshold_same DOUBLE,
            link_by_code BOOLEAN, link_by_name BOOLEAN, link_mode VARCHAR
        )
    """)
    if pairs_out:
        conn.executemany(
            f'INSERT INTO "{name}_03a" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', pairs_out
        )
    if poly_rows:
        conn.executemany(f'INSERT INTO "{name}_03b" VALUES (?, ?, ?, ?)', poly_rows)
    if changelog_rows:
        conn.executemany(
            f'INSERT INTO "{name}_03c" VALUES '
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            changelog_rows,
        )

    logger.info(
        "change: %d clusters (%d pairs, %d singletons)",
        len(cluster_members),
        len(pairs_out),
        sum(1 for a, b in cluster_members.values() if not a or not b),
    )
