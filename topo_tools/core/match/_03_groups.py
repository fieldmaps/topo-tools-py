"""Runs extend's pipeline once per parent group, in an isolated subprocess.

Each group's lines/attempt/merge run in a fresh, spawned OS process, not the
parent's shared connection -- CLAUDE.md documents a real, previously-confirmed
finding that GEOS's native heap isn't fully released between files even after
closing the DuckDB connection, which is exactly why extend() processes one
file per OS process today. A many-parent-group match() run would otherwise
hit the same failure mode in-process, just with groups substituting for
files. Data crosses the process boundary as small Parquet files, never a
shared connection (DuckDB files are single-writer).
"""

import contextlib
import multiprocessing
import shutil
from logging import INFO, basicConfig, getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.duckdb_utils import get_connection, log_file
from topo_tools.core.extend import _02_lines as lines
from topo_tools.core.extend import _05_merge as merge
from topo_tools.core.extend import attempt
from topo_tools.core.match._clip import clip_to_parent_geom

logger = getLogger(__name__)


def list_groups(conn: DuckDBPyConnection, name: str) -> list[int]:
    """Distinct assigned parent fids, ascending -- deterministic iteration order."""
    rows = conn.execute(f"""--sql
        SELECT DISTINCT parent_fid FROM "{name}_02_assign" ORDER BY parent_fid
    """).fetchall()
    return [row[0] for row in rows]


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    tmp_dir: Path,
    *,
    memory_gb: float,
    threads: int | None,
    debug: bool = False,
) -> None:
    """Loop over all groups sequentially, each isolated in its own subprocess."""
    ctx = multiprocessing.get_context("spawn")

    for parent_fid in list_groups(conn, name):
        gname = f"{name}_g{parent_fid}"
        group_dir = tmp_dir / gname
        group_dir.mkdir(parents=True, exist_ok=True)

        conn.execute(f"""--sql
            COPY (
                SELECT * FROM "{name}_child_01"
                WHERE fid IN (
                    SELECT child_fid FROM "{name}_02_assign"
                    WHERE parent_fid = {parent_fid}
                )
            ) TO '{group_dir / "child.parquet"}' (FORMAT PARQUET)
        """)
        conn.execute(f"""--sql
            COPY (SELECT geom FROM "{name}_parent_01" WHERE fid = {parent_fid})
            TO '{group_dir / "parent.parquet"}' (FORMAT PARQUET)
        """)

        result_queue = ctx.Queue()
        process = ctx.Process(
            target=_group_worker,
            args=(group_dir, memory_gb, threads, debug, result_queue),
        )
        process.start()
        process.join()

        # A freshly-spawned process has no logging config of its own (spawn
        # re-imports everything from scratch; basicConfig only ever runs in
        # cli/main.py, in the parent) -- an exception raised inside the
        # worker would otherwise vanish silently instead of surfacing here.
        # The worker puts an error string (or None on success) on the queue
        # instead of relying on its own logging output.
        err = (
            result_queue.get()
            if not result_queue.empty()
            else (
                f"worker exited with no result "
                f"(exitcode={process.exitcode}, likely killed/OOM)"
            )
        )
        output_path = group_dir / "output.parquet"
        if process.exitcode != 0 or err or not output_path.exists():
            logger.error(
                "match: group parent_fid=%s failed -- dropping its children from "
                "the output. exitcode=%s error=%s (see %s for exported inputs)",
                parent_fid,
                process.exitcode,
                err,
                group_dir,
            )
            continue

        _append_to_reassembly(conn, name, output_path)

        if not debug:
            shutil.rmtree(group_dir, ignore_errors=True)

    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [f"{name}_03"]
    ).fetchone()
    if exists is None:
        msg = f"match: no group produced any output for {name}"
        raise RuntimeError(msg)


def _append_to_reassembly(
    conn: DuckDBPyConnection, name: str, output_path: Path
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [f"{name}_03"]
    ).fetchone()
    if exists is None:
        conn.execute(f"""--sql
            CREATE TABLE "{name}_03" AS SELECT * FROM read_parquet('{output_path}')
        """)
    else:
        conn.execute(f"""--sql
            INSERT INTO "{name}_03" SELECT * FROM read_parquet('{output_path}')
        """)


def _group_worker(
    group_dir: Path,
    memory_gb: float,
    threads: int | None,
    debug: bool,  # noqa: FBT001
    result_queue: multiprocessing.Queue,
) -> None:
    """Child-process entry point; must stay module-level for spawn picklability.

    A freshly-spawned process has no logging config of its own -- basicConfig
    only ever runs in cli/main.py, in the parent -- so under --debug this sets
    it up locally (mirroring cli/main.py's own call) and tees to a per-group
    log file, otherwise ProfiledConnection's per-query timing/RSS output would
    be silently dropped instead of landing anywhere inspectable.
    """
    if debug:
        basicConfig(
            level=INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    try:
        with log_file("group", group_dir) if debug else contextlib.nullcontext():
            conn = get_connection("group", group_dir, threads=threads, debug=debug)
            conn.execute(f"""--sql
                CREATE TABLE "group_01" AS
                SELECT * FROM read_parquet('{group_dir / "child.parquet"}')
            """)  # already reprojected/coverage-cleaned by match's own inputs stage

            lines.main(conn, "group")
            attempt.main(conn, "group", memory_gb=memory_gb, debug=debug)
            merge.main(conn, "group", debug=debug)  # -> "group_05"

            clip_to_parent_geom(
                conn, "group_05", group_dir / "parent.parquet", "group_clip"
            )

            conn.execute(f"""--sql
                COPY (SELECT * FROM "group_clip")
                TO '{group_dir / "output.parquet"}' (FORMAT PARQUET)
            """)
            conn.close()
        result_queue.put(None)
    except Exception as e:  # noqa: BLE001 -- must not raise across the process boundary uncaught
        result_queue.put(f"{type(e).__name__}: {e}")
