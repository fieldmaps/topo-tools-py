"""Shared utilities: DuckDB connection and pipeline helpers."""

import contextlib
import re
import threading
import time
from collections.abc import Iterator
from logging import FileHandler, Formatter, getLogger

import psutil
from duckdb import DuckDBPyConnection
from duckdb import connect as duckdb_connect

from .config import debug, num_threads, tmp_dir

_PROCESS = psutil.Process()
logger = getLogger(__name__)

_GEO_PARQUET = (
    "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2')"
)
_PARQUET = "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15)"
_MEM_Q = "SELECT COALESCE(SUM(memory_usage_bytes), 0) FROM duckdb_memory()"


class ProfiledConnection:
    """Proxy around DuckDBPyConnection; logs RSS peak and duckdb delta per execute().

    RSS peak is the primary metric for Docker/WASM sizing — it captures GEOS working
    memory (ST_VoronoiDiagram, ST_Node, ST_Polygonize) that duckdb_memory() misses.
    duckdb delta/total are shown as secondary context for table accumulation.
    """

    def __init__(self, conn: DuckDBPyConnection) -> None:  # noqa: D107
        self._conn = conn

    def execute(self, query: str, parameters: list | None = None):  # noqa: ANN201
        """Log wall-clock time, RSS peak, and duckdb delta/total, then forward."""
        if not debug:
            return (
                self._conn.execute(query, parameters)
                if parameters is not None
                else self._conn.execute(query)
            )
        t0 = time.perf_counter()
        before_rss = _PROCESS.memory_info().rss
        before_ddb = self._conn.execute(_MEM_Q).fetchall()[0][0]

        peak_rss = [before_rss]
        stop = threading.Event()

        def _poll() -> None:
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    peak_rss[0] = max(peak_rss[0], _PROCESS.memory_info().rss)
                stop.wait(0.05)

        threading.Thread(target=_poll, daemon=True).start()

        result = (
            self._conn.execute(query, parameters)
            if parameters is not None
            else self._conn.execute(query)
        )
        # Materialize before the after-memory queries, which would otherwise
        # invalidate the result cursor on the same connection.
        rows = result.fetchall()

        stop.set()
        after_rss = _PROCESS.memory_info().rss
        peak_rss[0] = max(peak_rss[0], after_rss)
        after_ddb = self._conn.execute(_MEM_Q).fetchall()[0][0]

        elapsed = time.perf_counter() - t0
        logger.info(
            "query %.3fs | rss peak %.0f MB | duckdb %+.0f MB | %.0f MB total | %s",
            elapsed,
            peak_rss[0] / 1e6,
            (after_ddb - before_ddb) / 1e6,
            after_ddb / 1e6,
            _query_label(query),
        )
        return _EagerResult(rows)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __getattr__(self, name: str) -> object:  # noqa: D105
        return getattr(self._conn, name)


@contextlib.contextmanager
def log_file(name: str) -> Iterator[None]:
    """Tee root logger to tmp/{name}.log for every run."""
    tmp_dir.mkdir(exist_ok=True, parents=True)
    handler = FileHandler(tmp_dir / f"{name}.log", mode="w")
    handler.setFormatter(Formatter("%(asctime)s - %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()


def get_connection(name: str) -> ProfiledConnection:
    """Create a file-backed DuckDB connection with spatial loaded."""
    conn = duckdb_connect(str(tmp_dir / f"{name}.duckdb"))
    conn.execute("LOAD spatial")
    conn.execute("SET enable_progress_bar = false")
    conn.execute("SET geometry_always_xy = true")
    conn.execute("SET preserve_insertion_order = false")
    if num_threads is not None:
        conn.execute(f"SET threads = {num_threads}")
    return ProfiledConnection(conn)


def has_coverage_violations(conn: DuckDBPyConnection, table: str) -> bool:
    """Return True if `table.geom` has any overlaps or unmatched shared edges."""
    return conn.execute(f"""--sql
        SELECT ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL
        FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
    """).fetchall()[0][0]


def coverage_clean(
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    fids: list[int] | None,
    gap_max_width: float | None,
) -> None:
    """Write table_out from table_in with ST_CoverageClean applied to a subset (or all).

    ``fids=None`` runs the clean over the entire table. ``gap_max_width``
    enables the gap-merge parameter on the 3-arg overload; otherwise the
    1-arg overload is used.

    Mapping back to source rows: ST_CoverageClean returns a GeometryCollection
    whose i-th element corresponds to input i. ST_Dump recursively unnests
    sub-polygons of MultiPolygon elements, so we group by ``path[1]`` (the
    top-level index) and re-aggregate. A group of size 1 keeps the original
    Polygon type; a group of size >1 (MultiPolygon input/output) is re-wrapped
    via ST_Collect.
    """
    where = "" if fids is None else f"WHERE fid IN ({','.join(str(f) for f in fids)})"
    # snap_distance=-1 keeps GEOS's auto-detect default (which the 1-arg
    # overload also uses); 0.0 would explicitly disable snapping and produce
    # different output than the prior ctypes path that left the param unset.
    cc = (
        "ST_CoverageClean(list(geom ORDER BY fid))"
        if gap_max_width is None
        else f"ST_CoverageClean(list(geom ORDER BY fid), -1.0, {gap_max_width})"
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        WITH ord AS (
            SELECT fid, row_number() OVER (ORDER BY fid) AS rn
            FROM "{table_in}" {where}
        ),
        coll AS (
            SELECT {cc} AS g FROM "{table_in}" {where}
        ),
        dumped AS (
            SELECT (d).path[1] AS rn, (d).geom AS sub
            FROM (SELECT UNNEST(ST_Dump(g)) AS d FROM coll)
        ),
        grouped AS (
            SELECT rn, list(sub) AS subs FROM dumped GROUP BY rn
        ),
        parts AS (
            SELECT rn,
                   CASE WHEN len(subs) = 1 THEN subs[1] ELSE ST_Collect(subs) END
                       AS cleaned_geom
            FROM grouped
        ),
        mapping AS (
            SELECT ord.fid, parts.cleaned_geom
            FROM ord JOIN parts USING (rn)
        )
        SELECT t.* EXCLUDE (geom),
               COALESCE(m.cleaned_geom, t.geom) AS geom
        FROM "{table_in}" t
        LEFT JOIN mapping m USING (fid)
    """)


def cleanup_tmp(name: str, *, parquet: bool = False) -> None:
    """Remove tmp files for a named pipeline run."""
    for p in tmp_dir.glob(f"{name}.duckdb*"):
        p.unlink(missing_ok=True)
    if parquet:
        for p in tmp_dir.glob(f"{name}*.parquet"):
            p.unlink(missing_ok=True)


def export_debug_tables(conn: DuckDBPyConnection, only: set[str] | None = None) -> None:
    """Export pipeline tables to Parquet files for inspection."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    for (table,) in tables:
        if only is not None and table not in only:
            continue
        out = str(tmp_dir / f"{table}.parquet")
        has_geom = conn.execute(
            "SELECT COUNT(*) > 0 FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND data_type = 'GEOMETRY'"
        ).fetchall()[0][0]
        opts = _GEO_PARQUET if has_geom else _PARQUET
        conn.execute(f"COPY \"{table}\" TO '{out}' {opts}")


class _EagerResult:
    """Materialized DuckDB result that survives subsequent execute() calls."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self._idx = 0

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self) -> tuple | None:
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None


def _query_label(query: str) -> str:
    q = " ".join(query.split())
    m = re.search(r'CREATE (?:OR REPLACE )?TABLE "([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"CREATE {m.group(1)}"
    m = re.search(r'DROP TABLE (?:IF EXISTS )?"([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"DROP {m.group(1)}"
    m = re.search(r'ALTER TABLE "[^"]+" RENAME TO "([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"RENAME TO {m.group(1)}"
    m = re.search(r"COPY .+? TO '([^']+)'", q, re.IGNORECASE)
    if m:
        return f"COPY {m.group(1)}"
    return q[:80]
