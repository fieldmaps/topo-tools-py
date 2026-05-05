"""Cleans coverage topology violations in _01 via GEOS GEOSCoverageClean_r.

DuckDB spatial 1.5.2 doesn't expose ST_CoverageClean
(tracked at duckdb/duckdb-spatial#679), and Shapely 2.1 doesn't expose it
either. This module binds GEOS 3.14's GEOSCoverageClean_r directly via
ctypes so the algorithm can run without waiting on either upstream.

Runs only when ST_CoverageInvalidEdges_Agg detects topology violations on
_01; otherwise the stage is a no-op.

When the coverage contains sliver gaps (interior rings with low Polsby-
Popper compactness), gapMaximumWidth is auto-derived from the data: it
catches every thin gap while preserving any round feature (lake, etc.).
Compactness < 0.5 separates slivers from real features cleanly because
the lowest plausible compactness for a real feature is an equilateral
triangle at ~0.6.
"""

import ctypes
import os
import platform
from ctypes import (
    POINTER,
    Array,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_size_t,
    c_uint,
    c_void_p,
)
from functools import cache
from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from .utils import has_coverage_violations

logger = getLogger(__name__)

_GEOS_GEOMETRYCOLLECTION = 7
_MIN_GEOS_VERSION = (3, 14, 0)
_SLIVER_COMPACTNESS_MAX = 0.5
# Maximum sliver width as a fraction of the input coverage's bbox diagonal.
# Digitization noise scales with the spatial extent of the dataset (a Congo-
# scale layer can have 30m slivers; a city-scale layer has cm-scale ones), so
# the threshold has to scale too. A flat absolute would either miss real
# slivers in continental data or fill real holes in small-extent data. 1e-5
# is calibrated against ~30m slivers in Congo (bbox diagonal ~3000km).
_SLIVER_MAX_WIDTH_FRACTION = 1e-5


def _candidate_paths() -> list[str]:
    if env := os.environ.get("LIBGEOS_PATH"):
        return [env]
    if platform.system() == "Darwin":
        return [
            "/opt/homebrew/lib/libgeos_c.dylib",
            "/usr/local/lib/libgeos_c.dylib",
        ]
    return [
        "/usr/local/lib/libgeos_c.so.1",
        "/usr/local/lib/libgeos_c.so",
        "/usr/lib/x86_64-linux-gnu/libgeos_c.so.1",
        "/usr/lib/aarch64-linux-gnu/libgeos_c.so.1",
        "/usr/lib/libgeos_c.so.1",
    ]


def _load_libgeos() -> ctypes.CDLL:
    last_error: OSError | None = None
    for path in _candidate_paths():
        if Path(path).exists():
            try:
                return ctypes.CDLL(path)
            except OSError as e:
                last_error = e
    msg = (
        "libgeos>=3.14 not found. Set LIBGEOS_PATH to a libgeos_c shared "
        f"library. Last error: {last_error}"
    )
    raise RuntimeError(msg)


def _parse_version(raw: bytes) -> tuple[int, int, int]:
    parts = raw.decode().split("-")[0].split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _bind(lib: ctypes.CDLL) -> None:
    lib.GEOS_init_r.argtypes = []
    lib.GEOS_init_r.restype = c_void_p
    lib.GEOS_finish_r.argtypes = [c_void_p]
    lib.GEOS_finish_r.restype = None

    lib.GEOSWKBReader_create_r.argtypes = [c_void_p]
    lib.GEOSWKBReader_create_r.restype = c_void_p
    lib.GEOSWKBReader_destroy_r.argtypes = [c_void_p, c_void_p]
    lib.GEOSWKBReader_destroy_r.restype = None
    lib.GEOSWKBReader_read_r.argtypes = [c_void_p, c_void_p, c_char_p, c_size_t]
    lib.GEOSWKBReader_read_r.restype = c_void_p

    lib.GEOSWKBWriter_create_r.argtypes = [c_void_p]
    lib.GEOSWKBWriter_create_r.restype = c_void_p
    lib.GEOSWKBWriter_destroy_r.argtypes = [c_void_p, c_void_p]
    lib.GEOSWKBWriter_destroy_r.restype = None
    lib.GEOSWKBWriter_write_r.argtypes = [
        c_void_p,
        c_void_p,
        c_void_p,
        POINTER(c_size_t),
    ]
    lib.GEOSWKBWriter_write_r.restype = POINTER(c_char)

    lib.GEOSGeom_destroy_r.argtypes = [c_void_p, c_void_p]
    lib.GEOSGeom_destroy_r.restype = None
    lib.GEOSGeom_createCollection_r.argtypes = [
        c_void_p,
        c_int,
        POINTER(c_void_p),
        c_uint,
    ]
    lib.GEOSGeom_createCollection_r.restype = c_void_p
    lib.GEOSGetNumGeometries_r.argtypes = [c_void_p, c_void_p]
    lib.GEOSGetNumGeometries_r.restype = c_int
    lib.GEOSGetGeometryN_r.argtypes = [c_void_p, c_void_p, c_int]
    lib.GEOSGetGeometryN_r.restype = c_void_p

    lib.GEOSCoverageClean_r.argtypes = [c_void_p, c_void_p]
    lib.GEOSCoverageClean_r.restype = c_void_p

    lib.GEOSCoverageCleanParams_create_r.argtypes = [c_void_p]
    lib.GEOSCoverageCleanParams_create_r.restype = c_void_p
    lib.GEOSCoverageCleanParams_destroy_r.argtypes = [c_void_p, c_void_p]
    lib.GEOSCoverageCleanParams_destroy_r.restype = None
    lib.GEOSCoverageCleanParams_setGapMaximumWidth_r.argtypes = [
        c_void_p,
        c_void_p,
        ctypes.c_double,
    ]
    lib.GEOSCoverageCleanParams_setGapMaximumWidth_r.restype = c_int
    lib.GEOSCoverageCleanParams_setSnappingDistance_r.argtypes = [
        c_void_p,
        c_void_p,
        ctypes.c_double,
    ]
    lib.GEOSCoverageCleanParams_setSnappingDistance_r.restype = c_int
    lib.GEOSCoverageCleanWithParams_r.argtypes = [c_void_p, c_void_p, c_void_p]
    lib.GEOSCoverageCleanWithParams_r.restype = c_void_p

    lib.GEOSSnap_r.argtypes = [c_void_p, c_void_p, c_void_p, ctypes.c_double]
    lib.GEOSSnap_r.restype = c_void_p

    lib.GEOSFree_r.argtypes = [c_void_p, c_void_p]
    lib.GEOSFree_r.restype = None


@cache
def _get_lib() -> ctypes.CDLL:
    lib = _load_libgeos()
    lib.GEOSversion.argtypes = []
    lib.GEOSversion.restype = c_char_p
    version = _parse_version(lib.GEOSversion())
    if version < _MIN_GEOS_VERSION:
        msg = (
            f"libgeos {version} found; need >= {_MIN_GEOS_VERSION} for "
            "GEOSCoverageClean. Set LIBGEOS_PATH to a newer libgeos_c."
        )
        raise RuntimeError(msg)
    _bind(lib)
    return lib


def _check_ptr(ptr: int | None, what: str) -> int:
    if not ptr:
        msg = f"{what} returned NULL"
        raise RuntimeError(msg)
    return ptr


def _run_coverage_clean(
    lib: ctypes.CDLL,
    handle: int,
    coll: int,
    gap_max_width: float | None,
    snap_distance: float | None,
) -> tuple[int, int]:
    """Invoke the appropriate GEOSCoverageClean variant; return (cleaned, params)."""
    if gap_max_width is None and snap_distance is None:
        cleaned = _check_ptr(
            lib.GEOSCoverageClean_r(handle, coll), "GEOSCoverageClean_r"
        )
        return cleaned, 0
    params = _check_ptr(
        lib.GEOSCoverageCleanParams_create_r(handle),
        "GEOSCoverageCleanParams_create_r",
    )
    if (
        gap_max_width is not None
        and not lib.GEOSCoverageCleanParams_setGapMaximumWidth_r(
            handle, params, gap_max_width
        )
    ):
        msg = "GEOSCoverageCleanParams_setGapMaximumWidth_r failed"
        raise RuntimeError(msg)
    if (
        snap_distance is not None
        and not lib.GEOSCoverageCleanParams_setSnappingDistance_r(
            handle, params, snap_distance
        )
    ):
        msg = "GEOSCoverageCleanParams_setSnappingDistance_r failed"
        raise RuntimeError(msg)
    cleaned = _check_ptr(
        lib.GEOSCoverageCleanWithParams_r(handle, coll, params),
        "GEOSCoverageCleanWithParams_r",
    )
    return cleaned, params


def _read_wkbs(lib: ctypes.CDLL, handle: int, reader: int, wkbs: list[bytes]) -> Array:
    """Decode each WKB into a GEOS geometry pointer; return ctypes array."""
    geom_ptrs = (c_void_p * len(wkbs))()
    for i, wkb in enumerate(wkbs):
        geom_ptrs[i] = _check_ptr(
            lib.GEOSWKBReader_read_r(handle, reader, wkb, len(wkb)),
            f"GEOSWKBReader_read_r[{i}]",
        )
    return geom_ptrs


def _write_wkbs(
    lib: ctypes.CDLL, handle: int, writer: int, cleaned: int
) -> list[bytes]:
    """Serialize each sub-geometry of `cleaned` back to WKB."""
    n_out = lib.GEOSGetNumGeometries_r(handle, cleaned)
    out: list[bytes] = []
    size = c_size_t(0)
    for i in range(n_out):
        sub = _check_ptr(
            lib.GEOSGetGeometryN_r(handle, cleaned, i), f"GEOSGetGeometryN_r[{i}]"
        )
        buf = _check_ptr(
            lib.GEOSWKBWriter_write_r(handle, writer, sub, byref(size)),
            f"GEOSWKBWriter_write_r[{i}]",
        )
        try:
            out.append(ctypes.string_at(buf, size.value))
        finally:
            lib.GEOSFree_r(handle, buf)
    return out


def _release(  # noqa: PLR0913
    lib: ctypes.CDLL,
    handle: int,
    *,
    writer: int,
    reader: int,
    cleaned: int,
    params: int,
    coll: int,
    geom_ptrs: Array,
) -> None:
    """Destroy GEOS resources allocated during _clean_wkbs."""
    if writer:
        lib.GEOSWKBWriter_destroy_r(handle, writer)
    if reader:
        lib.GEOSWKBReader_destroy_r(handle, reader)
    if cleaned:
        lib.GEOSGeom_destroy_r(handle, cleaned)
    if params:
        lib.GEOSCoverageCleanParams_destroy_r(handle, params)
    if coll:
        lib.GEOSGeom_destroy_r(handle, coll)
    else:
        for ptr in geom_ptrs:
            if ptr:
                lib.GEOSGeom_destroy_r(handle, ptr)
    lib.GEOS_finish_r(handle)


def _clean_wkbs(
    wkbs: list[bytes],
    gap_max_width: float | None = None,
    snap_distance: float | None = None,
) -> list[bytes]:
    """Pass WKBs through GEOSCoverageClean_r and return cleaned WKBs.

    The output preserves input element count and order: cleaned[i] is the
    cleaned form of input[i] (possibly empty if absorbed by a neighbour).

    When gap_max_width is set, GEOSCoverageCleanWithParams_r merges
    fully-enclosed gaps narrower than gap_max_width into the adjacent
    polygon with the longest shared border.

    When snap_distance is set, the same call snaps nearby vertices/edges
    together, harmonizing adjacent polygon boundaries that touch but
    don't share identical vertex paths (the "wiggle" pattern). Both
    parameters can be combined in one pass.
    """
    lib = _get_lib()
    handle = _check_ptr(lib.GEOS_init_r(), "GEOS_init_r")

    reader = writer = coll = cleaned = params = 0
    geom_ptrs = (c_void_p * len(wkbs))()

    try:
        reader = _check_ptr(
            lib.GEOSWKBReader_create_r(handle), "GEOSWKBReader_create_r"
        )
        geom_ptrs = _read_wkbs(lib, handle, reader, wkbs)
        coll = _check_ptr(
            lib.GEOSGeom_createCollection_r(
                handle, _GEOS_GEOMETRYCOLLECTION, geom_ptrs, len(wkbs)
            ),
            "GEOSGeom_createCollection_r",
        )
        cleaned, params = _run_coverage_clean(
            lib, handle, coll, gap_max_width, snap_distance
        )
        n_out = lib.GEOSGetNumGeometries_r(handle, cleaned)
        if n_out != len(wkbs):
            msg = (
                f"GEOSCoverageClean returned {n_out} geometries, "
                f"expected {len(wkbs)} (input element ordering broken)"
            )
            raise RuntimeError(msg)
        writer = _check_ptr(
            lib.GEOSWKBWriter_create_r(handle), "GEOSWKBWriter_create_r"
        )
        return _write_wkbs(lib, handle, writer, cleaned)
    finally:
        _release(
            lib,
            handle,
            writer=writer,
            reader=reader,
            cleaned=cleaned,
            params=params,
            coll=coll,
            geom_ptrs=geom_ptrs,
        )


def _sliver_info(conn: DuckDBPyConnection, name: str) -> tuple[float | None, list[int]]:
    """Detect sliver gaps; return (max_width, bordering_fids).

    A sliver gap is an interior ring of ST_Union_Agg(_01) that is BOTH
    irregularly-shaped (Polsby-Popper compactness < 0.5, below the lowest
    plausible compactness for a real polygonal feature — equilateral triangle
    ≈ 0.6) AND narrow relative to the dataset's spatial extent (max width
    under _SLIVER_MAX_WIDTH_FRACTION of the union's bbox diagonal). The
    width threshold scales with the bbox diagonal because digitization noise
    scales with the dataset: a Congo-sized layer can have 30m slivers; a
    city-sized layer has cm-scale ones. Real internal holes (lakes, enclaves,
    disputed territories) sit well above this scaled threshold and must be
    left as gaps for the Voronoi extension to divide across bordering
    polygons — filling them here collapses the whole hole onto one neighbour.

    The union is unnested into single-polygon parts before counting interior
    rings; ST_NumInteriorRings on a MultiPolygon returns 0, so a coverage
    that splits into multiple parts (e.g. mainland + offshore islet) would
    otherwise hide every interior-ring gap.

    Returns (None, []) when no slivers exist.
    """
    row = conn.execute(f"""--sql
        WITH
        u AS (SELECT ST_Union_Agg(geom) AS g FROM "{name}_01"),
        extent AS (
            SELECT sqrt(power(ST_XMax(g) - ST_XMin(g), 2)
                      + power(ST_YMax(g) - ST_YMin(g), 2))
                   * {_SLIVER_MAX_WIDTH_FRACTION} AS max_sliver_width
            FROM u
        ),
        parts AS (SELECT UNNEST(ST_Dump(g)).geom AS p FROM u),
        rings AS (
            SELECT UNNEST(generate_series(1, ST_NumInteriorRings(p))) AS i, p
            FROM parts
        ),
        gaps AS (
            SELECT ST_MakePolygon(ST_InteriorRingN(p, i)) AS gap FROM rings
        ),
        slivers AS (
            SELECT gap, width FROM (
                SELECT
                    gap,
                    ST_MaximumInscribedCircle(gap, 1e-9).radius * 2 AS width
                FROM gaps
                WHERE 4 * pi() * ST_Area(gap)
                    / (ST_Perimeter(gap) * ST_Perimeter(gap))
                    < {_SLIVER_COMPACTNESS_MAX}
            )
            WHERE width < (SELECT max_sliver_width FROM extent)
        )
        SELECT max(s.width), list(DISTINCT i.fid)
        FROM slivers s, "{name}_01" i
        WHERE ST_Intersects(i.geom, s.gap)
    """).fetchone()
    max_width, fids = row
    if max_width is None:
        return None, []
    fids = sorted(fids or [])
    logger.info(
        "auto-detected sliver gap(s) bordering %d feature(s); gap_max_width=%.3e",
        len(fids),
        max_width,
    )
    return float(max_width), fids


def _violator_fids(conn: DuckDBPyConnection, name: str) -> list[int]:
    """Return fids of polygons that touch any invalid coverage edge.

    `ST_CoverageInvalidEdges_Agg` returns a multilinestring whose non-empty
    parts are the edges where the coverage breaks. A polygon "owns" a
    violation if its boundary intersects any non-empty invalid edge.
    """
    return [
        r[0]
        for r in conn.execute(f"""--sql
        WITH
        bad AS (
            SELECT ST_CoverageInvalidEdges_Agg(geom) AS edges
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{name}_01")
        ),
        edges AS (
            SELECT UNNEST(ST_Dump(edges)).geom AS edge FROM bad
        ),
        real_edges AS (
            SELECT edge FROM edges
            WHERE NOT ST_IsEmpty(edge) AND ST_NPoints(edge) > 0
        )
        SELECT DISTINCT i.fid
        FROM "{name}_01" i, real_edges e
        WHERE ST_Intersects(i.geom, e.edge)
        ORDER BY i.fid
    """).fetchall()
    ]


def _apply_cleaned(
    conn: DuckDBPyConnection,
    name: str,
    fids: list[int],
    cleaned_wkbs: list[bytes],
) -> None:
    """Replace the geom of the listed fids in _01 with the cleaned WKBs."""
    conn.execute(
        f'CREATE OR REPLACE TEMP TABLE "{name}_clean_tmp" (fid BIGINT, wkb BLOB)'
    )
    conn.executemany(
        f'INSERT INTO "{name}_clean_tmp" VALUES (?, ?)',
        list(zip(fids, cleaned_wkbs, strict=True)),
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_01" AS
        SELECT t.* EXCLUDE (geom),
               COALESCE(ST_GeomFromWKB(c.wkb), t.geom) AS geom
        FROM "{name}_01" t
        LEFT JOIN "{name}_clean_tmp" c USING (fid)
    """)
    conn.execute(f'DROP TABLE "{name}_clean_tmp"')


def _global_clean(conn: DuckDBPyConnection, name: str) -> None:
    """Run GEOSCoverageClean on the entire _01 in one pass and write back.

    Used as a fallback when surgical cleaning fails to converge. This is the
    original (pre-surgical) behaviour: every polygon in _01 is sent through
    coverage clean, so every polygon's coordinates may shift.
    """
    gap_max_width, _ = _sliver_info(conn, name)
    rows = conn.execute(f"""--sql
        SELECT fid, ST_AsWKB(geom)::BLOB FROM "{name}_01" ORDER BY fid
    """).fetchall()
    fids = [r[0] for r in rows]
    wkbs = [bytes(r[1]) for r in rows]
    logger.info("global clean: %d feature(s) via GEOSCoverageClean", len(wkbs))
    cleaned_wkbs = _clean_wkbs(wkbs, gap_max_width=gap_max_width)
    _apply_cleaned(conn, name, fids, cleaned_wkbs)


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Clean coverage topology violations and sliver gaps in _01 surgically.

    Identifies the subset of polygons that own invalid edges or border a
    sliver gap, runs GEOSCoverageClean on that subset only, and splices the
    result back into _01. Polygons not in the subset stay byte-identical
    to input.

    Sliver gaps (interior rings in the unioned coverage with very low
    Polsby-Popper compactness) are not invalid edges, so
    ST_CoverageInvalidEdges_Agg won't flag them. They still break the
    pipeline downstream (lines.main can't extract a shared edge that
    doesn't exist as coincident geometry, and merge.main fuses the
    sliver-bounding polygons into one cell), so they need cleaning too.

    If the surgical pass leaves residual violations (e.g. cleaning created
    new ones at the boundary with unchanged neighbours), restore _01 from
    a snapshot and fall back to a single full-coverage clean — better to
    take the global drift hit once than ship a half-cleaned coverage.
    """
    invalid_fids = (
        _violator_fids(conn, name)
        if has_coverage_violations(conn, f"{name}_01")
        else []
    )
    gap_max_width, sliver_fids = _sliver_info(conn, name)
    violators = sorted(set(invalid_fids) | set(sliver_fids))
    if not violators:
        return

    conn.execute(
        f'CREATE OR REPLACE TABLE "{name}_01_snapshot" AS SELECT * FROM "{name}_01"'
    )

    fid_list = ",".join(str(f) for f in violators)
    rows = conn.execute(f"""--sql
        SELECT fid, ST_AsWKB(geom)::BLOB
        FROM "{name}_01"
        WHERE fid IN ({fid_list})
        ORDER BY fid
    """).fetchall()
    fids = [r[0] for r in rows]
    wkbs = [bytes(r[1]) for r in rows]
    logger.info(
        "surgical clean: %d violator(s) (%d invalid-edge, %d sliver) via "
        "GEOSCoverageClean",
        len(violators),
        len(invalid_fids),
        len(sliver_fids),
    )
    cleaned_wkbs = _clean_wkbs(wkbs, gap_max_width=gap_max_width)
    _apply_cleaned(conn, name, fids, cleaned_wkbs)

    residual_invalid = has_coverage_violations(conn, f"{name}_01")
    _, residual_slivers = _sliver_info(conn, name)
    if residual_invalid or residual_slivers:
        logger.warning(
            "surgical clean did not converge (invalid_edges=%s, slivers=%d); "
            "restoring _01 and falling back to full-coverage clean",
            residual_invalid,
            len(residual_slivers),
        )
        conn.execute(
            f'CREATE OR REPLACE TABLE "{name}_01" AS SELECT * FROM "{name}_01_snapshot"'
        )
        _global_clean(conn, name)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_01_snapshot"')
