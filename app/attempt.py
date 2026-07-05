"""Retries points + voronoi stages with doubling distance on failure."""

from decimal import Decimal
from logging import getLogger

from duckdb import DuckDBPyConnection
from duckdb import Error as DuckDBError

from . import _03_points as points
from . import _04_voronoi as voronoi
from .config import (
    _BASELINE_OVERHEAD_MB,
    _BYTES_PER_POINT,
    _REMERGE_BYTES_PER_RAW_SEGMENT,
    _SAFETY_MARGIN,
    MAX_POINTS,
    debug,
    distance,
    memory_gb,
)

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Try to generate Voronoi polygons with multiple distance thresholds.

    The starting distance is derived per-file rather than always using the
    configured default: effective_distance = MAX(MIN(DISTANCE, natural_res),
    total_exterior_length / target_point_budget). natural_res (the median
    real segment length) lets files with genuinely finer source detail than
    DISTANCE start there instead of losing that detail to a coarser default;
    the budget term protects files whose exterior boundary would otherwise
    generate more points than --memory-gb can safely hold. Neither term can
    affect files whose segments are pathologically long — MAX_POINTS_PER_SEGMENT
    caps those independently of DISTANCE.

    Before any of that, raw_segment_count-driven memory (decomposing "{name}_02"
    into real segments, then remerging the normal ones per fid) is checked
    against memory_gb directly: this cost is DISTANCE-independent, so no
    amount of doubling DISTANCE in the retry loop below can rescue a file
    whose raw vertex count alone already exceeds the budget (confirmed on
    phl_admin3.parquet: OOM'd inside a real 4GB container decomposing 13M
    real segments, before DISTANCE was ever applied) — fail fast instead of
    wasting 10 retries and, for files big enough, a real memory-limit crash.

    If the effective distance still fails or produces too many points, repeat
    by doubling it up to 10 times, same fallback as before.
    """
    points.build_segments(conn, name)
    natural_res, total_length, raw_segment_count = conn.execute(f"""--sql
        SELECT median(seg_len), sum(seg_len), count(*) FROM "{name}_03_tmp1"
    """).fetchall()[0]

    if natural_res is None or total_length is None:
        effective_distance = Decimal(str(float(distance)))
        logger.info(
            "distance-calc: %s no real segments, using default=%s", name, distance
        )
    else:
        remerge_floor_mb = (
            raw_segment_count * _REMERGE_BYTES_PER_RAW_SEGMENT / 1_000_000
        )
        usable_mb = memory_gb * 1024 - _BASELINE_OVERHEAD_MB - remerge_floor_mb
        if usable_mb <= 0:
            msg = (
                f"{name}: {raw_segment_count:,} raw boundary segments need "
                f"~{remerge_floor_mb:.0f}MB to decompose and remerge alone, "
                f"which already exceeds the ~{memory_gb * 1024:.0f}MB budget "
                f"(--memory-gb={memory_gb}) before any DISTANCE is applied — "
                "no DISTANCE value can fix this, so not attempting"
            )
            logger.error(msg)
            raise RuntimeError(msg)

        target_point_budget = int(
            usable_mb * _SAFETY_MARGIN * 1_000_000 / _BYTES_PER_POINT
        )
        budget_floor = total_length / target_point_budget
        candidate = min(float(distance), natural_res)
        effective_distance = Decimal(str(max(candidate, budget_floor)))
        logger.info(
            "distance-calc: %s raw_segments=%s remerge_floor_mb=%.0f"
            " target_point_budget=%s natural_res=%s total_length=%s"
            " budget_floor=%s effective=%s",
            name,
            raw_segment_count,
            remerge_floor_mb,
            target_point_budget,
            natural_res,
            total_length,
            budget_floor,
            effective_distance,
        )

    try:
        for d in [effective_distance * 2**i for i in range(10)]:
            try:
                points.main(conn, name, d)
                count = conn.execute(f'SELECT count(*) FROM "{name}_03b"').fetchall()[
                    0
                ][0]
                _check_point_count(count)
                voronoi.main(conn, name)
            except (RuntimeError, DuckDBError) as e:
                logger.warning("fail: %s --distance=%s: %s", name, d, e)
            else:
                return
        error = f"{name} did not succeed generating voronoi polygons"
        logger.error(error)
        raise RuntimeError(error)
    finally:
        if not debug:
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp1"')


def _check_point_count(count: int) -> None:
    if count > MAX_POINTS:
        msg = f"too many points: {count:,}"
        raise RuntimeError(msg)
