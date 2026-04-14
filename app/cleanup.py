from pathlib import Path

import duckdb

from .utils import parquet


def main(_conn: duckdb.DuckDBPyConnection, name: str, *_: list) -> None:
    """Delete intermediate Parquet files."""
    for stage in ["attr", "01", "02", "03", "04", "05", "06"]:
        p = Path(parquet(f"{name}_{stage}"))
        if p.exists():
            p.unlink()
