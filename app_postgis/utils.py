import re
import sqlite3
from pathlib import Path
from subprocess import PIPE, run

from psycopg import connect

from .config import dbname


def get_gpkg_layers(file: Path) -> list[str]:
    """Get list of layers in GeoPackage."""
    query = """
        SELECT table_name, geometry_type_name
        FROM gpkg_geometry_columns
        WHERE geometry_type_name IN ('POLYGON', 'MULTIPOLYGON', 'GEOMETRY');
    """
    con = sqlite3.connect(file)
    cur = con.cursor()
    layers = sorted([row[0] for row in cur.execute(query)])
    cur.close()
    con.close()
    return layers


def is_polygon(file: Path) -> bool:
    """Check if file is a polygon."""
    regex = re.compile(r"\((?:Multi )?Polygon\)")
    result = run(
        ["gdal", "vector", "info", "--summary", file],
        check=False,
        stdout=PIPE,
    )
    return bool(regex.search(str(result.stdout)))


def apply_funcs(name: str, file: Path, layer: str, *args: list) -> None:
    """Apply functions to database."""
    conn = connect(f"dbname={dbname}", autocommit=True)
    for func in args:
        func(conn, name, file, layer)
    conn.close()
