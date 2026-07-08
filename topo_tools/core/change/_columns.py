"""Regex-based code/name column auto-detection.

Ported from topo-tools-js's src/lib/db/columns.ts -- same patterns, same
first-match-wins priority order.
"""

import re

from duckdb import DuckDBPyConnection

_CODE_PATTERNS = [
    re.compile(r"^(gid|fid)$", re.IGNORECASE),
    re.compile(
        r"^(geoid|pcode|adm[0-9]?_?pcode|p_code|iso3?|iso_a[23])$", re.IGNORECASE
    ),
    re.compile(r"^(fips|hasc|adm_code)$", re.IGNORECASE),
    re.compile(r"^(code|id)$", re.IGNORECASE),
    re.compile(r"code$", re.IGNORECASE),
]

_NAME_PATTERNS = [
    re.compile(r"^name$", re.IGNORECASE),
    re.compile(r"^(.*_name|name_.*)$", re.IGNORECASE),
    re.compile(r"^(label|title|display.*|short.*name|long.*name)$", re.IGNORECASE),
    re.compile(r"^adm[0-9]?_?(en|name)$", re.IGNORECASE),
]

# fid is topo-tools' own internal row_number column, geom is the internal
# geometry column -- neither is a candidate identity attribute.
_EXCLUDED_COLUMNS = {"fid", "geom"}


def _candidate_columns(conn: DuckDBPyConnection, table: str) -> list[str]:
    rows = conn.execute(f'DESCRIBE "{table}"').fetchall()
    return [row[0] for row in rows if row[0] not in _EXCLUDED_COLUMNS]


def _pick_first(columns: list[str], patterns: list[re.Pattern]) -> str | None:
    for pattern in patterns:
        hit = next((c for c in columns if pattern.search(c)), None)
        if hit:
            return hit
    return None


def detect_code_column(conn: DuckDBPyConnection, table: str) -> str | None:
    """Auto-detect a code/identifier column on `table`, or None if none matches."""
    return _pick_first(_candidate_columns(conn, table), _CODE_PATTERNS)


def detect_name_column(conn: DuckDBPyConnection, table: str) -> str | None:
    """Auto-detect a name/label column on `table`, or None if none matches."""
    return _pick_first(_candidate_columns(conn, table), _NAME_PATTERNS)
