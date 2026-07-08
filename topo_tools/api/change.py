"""Public API: compare two polygon layer versions and classify what changed."""

import shutil
import signal
import tempfile
from logging import getLogger
from pathlib import Path
from types import FrameType
from typing import NoReturn

from duckdb import DuckDBPyConnection

from topo_tools.core.change import _01_inputs as inputs
from topo_tools.core.change import _02_overlap as overlap
from topo_tools.core.change import _03_classify as classify
from topo_tools.core.change import _04_outputs as outputs
from topo_tools.core.change._columns import detect_code_column, detect_name_column
from topo_tools.core.change._constants import (
    TABLE_COPY_OPTS,
    TAU_MATCH_DEFAULT,
    TAU_SAME_DEFAULT,
)
from topo_tools.core.duckdb_utils import (
    cleanup_tmp,
    export_debug_tables,
    get_connection,
    log_file,
)
from topo_tools.core.extend._constants import COPY_OPTS

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "overlap", "classify", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_a_01", "{n}_b_01"],
    "overlap": ["{n}_02"],
    "classify": ["{n}_03a", "{n}_03b", "{n}_03c"],
    "outputs": [],
}


def _resolve_column(
    conn: DuckDBPyConnection, table: str, explicit: str | None, *, kind: str, side: str
) -> str | None:
    if explicit is not None:
        return explicit
    detector = detect_code_column if kind == "code" else detect_name_column
    column = detector(conn, table)
    if column is None:
        msg = (
            f"--link-by-{kind} was requested but no {kind} column could be "
            f"auto-detected on the {side} side; pass --{kind}-column-{side} explicitly"
        )
        raise ValueError(msg)
    return column


def change(  # noqa: C901, PLR0912, PLR0913, PLR0915
    old_path: str | Path,
    new_path: str | Path,
    output_path: str | Path | None = None,
    overlay_path: str | Path | None = None,
    *,
    tau_match: float = TAU_MATCH_DEFAULT,
    tau_same: float = TAU_SAME_DEFAULT,
    link_by_code: bool = False,
    link_by_name: bool = False,
    link_mode: str = "either",
    code_column_a: str | None = None,
    code_column_b: str | None = None,
    name_column_a: str | None = None,
    name_column_b: str | None = None,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = False,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Compare two polygon layer versions and classify every unit's relationship.

    Processes exactly one old file + one new file per call. Classifies each
    unit as unchanged/renamed/modified/relocated/split/merge/complex/created/
    removed, using spatial overlap (tau_match/tau_same) and, optionally,
    code/name identity linking. Always writes two files: a tabular changelog
    (output_path, CSV/Parquet, "_changelog" suffix if omitted) and a spatial
    overlay layer colored by relationship_class (overlay_path, "_overlay"
    suffix if omitted).
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)
    if link_mode not in ("either", "both"):
        msg = f"link_mode must be 'either' or 'both', got {link_mode!r}"
        raise ValueError(msg)

    old_path = Path(old_path)
    new_path = Path(new_path)
    output_path = (
        Path(output_path)
        if output_path is not None
        else old_path.parent / f"{old_path.stem}_{new_path.stem}_changelog.csv"
    )
    if output_path.suffix not in TABLE_COPY_OPTS:
        msg = (
            f"output file must be one of {sorted(TABLE_COPY_OPTS)} (a tabular "
            "format -- the changelog has no geometry column), got "
            f"{output_path.suffix!r}"
        )
        raise ValueError(msg)
    overlay_path = (
        Path(overlay_path)
        if overlay_path is not None
        else output_path.with_stem(output_path.stem + "_overlay").with_suffix(
            old_path.suffix
        )
    )
    if overlay_path.suffix not in COPY_OPTS:
        msg = (
            f"overlay file must be one of {sorted(COPY_OPTS)}, "
            f"got {overlay_path.suffix!r}"
        )
        raise ValueError(msg)
    if output_path.exists() and not overwrite:
        msg = f"output already exists: {output_path}"
        raise FileExistsError(msg)
    if overlay_path.exists() and not overwrite:
        msg = f"output already exists: {overlay_path}"
        raise FileExistsError(msg)

    owns_tmp_dir = tmp_dir is None
    tmp_dir_path = (
        Path(tmp_dir)
        if tmp_dir is not None
        else Path(tempfile.mkdtemp(prefix="topo_tools_"))
    )
    tmp_dir_path.mkdir(exist_ok=True, parents=True)

    # "_changelog" keeps every table/file this call creates distinct from an
    # extend()/match()/clean() run against the same old_path/tmp_dir, same
    # collision-avoidance reasoning as match's "_match" and clean's "_clean".
    name = old_path.name.replace(".", "_") + "_changelog"
    if not step:
        cleanup_tmp(name, tmp_dir_path, parquet=True)

    with log_file(name, tmp_dir_path):
        conn = get_connection(name, tmp_dir_path, threads=threads, debug=debug)

        def _interrupt(_sig: int, _frame: FrameType | None) -> NoReturn:
            conn.interrupt()
            raise KeyboardInterrupt

        old_handler = signal.signal(signal.SIGINT, _interrupt)
        try:
            logger.info("starting: %s", name)
            for s in _STEP_ORDER:
                if step and step != s:
                    continue
                if debug:
                    logger.info("=== %s ===", s)
                if s == "inputs":
                    inputs.main(conn, name, old_path, new_path)
                elif s == "overlap":
                    overlap.main(conn, name)
                elif s == "classify":
                    resolved_code_a = (
                        _resolve_column(
                            conn, f"{name}_a_01", code_column_a, kind="code", side="a"
                        )
                        if link_by_code
                        else code_column_a
                    )
                    resolved_code_b = (
                        _resolve_column(
                            conn, f"{name}_b_01", code_column_b, kind="code", side="b"
                        )
                        if link_by_code
                        else code_column_b
                    )
                    resolved_name_a = (
                        _resolve_column(
                            conn, f"{name}_a_01", name_column_a, kind="name", side="a"
                        )
                        if link_by_name
                        else name_column_a
                    )
                    resolved_name_b = (
                        _resolve_column(
                            conn, f"{name}_b_01", name_column_b, kind="name", side="b"
                        )
                        if link_by_name
                        else name_column_b
                    )
                    classify.main(
                        conn,
                        name,
                        tau_match=tau_match,
                        tau_same=tau_same,
                        link_by_code=link_by_code,
                        link_by_name=link_by_name,
                        link_mode=link_mode,
                        code_col_a=resolved_code_a,
                        code_col_b=resolved_code_b,
                        name_col_a=resolved_name_a,
                        name_col_b=resolved_name_b,
                    )
                elif s == "outputs":
                    outputs.main(conn, name, output_path, overlay_path, debug=debug)
            if debug:
                only = None
                if step and step in _STEP_TABLES:
                    only = {t.format(n=name) for t in _STEP_TABLES[step]}
                export_debug_tables(conn, tmp_dir_path, only=only)
            logger.info("done: %s", name)
        finally:
            signal.signal(signal.SIGINT, old_handler)
            conn.close()
            if not step and not debug:
                cleanup_tmp(name, tmp_dir_path)
            if owns_tmp_dir:
                if debug:
                    logger.info("tmp_dir preserved for --debug: %s", tmp_dir_path)
                else:
                    shutil.rmtree(tmp_dir_path, ignore_errors=True)
