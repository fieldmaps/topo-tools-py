"""Assigns each child polygon to the parent it shares the largest area with."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from ._constants import EQUAL_AREA_CRS

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Assign each child to its plurality-overlap parent; drop and log the rest.

    Bbox-prefiltered self-join across the two layers, not ST_Within/
    ST_Intersects in the JOIN condition -- that triggers DuckDB's SPATIAL_JOIN
    operator and its ~1x-RAM virtual reservation (see docs/topology.md). Both
    layers are exploded into parts first so a multi-part parent (e.g. a
    country with offshore islands) doesn't get one whole-fid bbox spanning
    everything and defeat the prefilter, same as _05_merge.py's _05_tmp1.
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_child_01"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp2" AS
        SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_parent_01"
    """)

    # Shared area per (child, parent) fid pair, summed across all part-pairs --
    # a multi-part child can overlap a multi-part parent in more than one
    # place. Ranked in an equal-area CRS; only the intersection geometry (not
    # the whole layer) is transformed, to bound the cost.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_pairs" AS
        SELECT c.fid AS child_fid, p.fid AS parent_fid,
               SUM(ST_Area(ST_Transform(
                   ST_Intersection(c.part_geom, p.part_geom),
                   'EPSG:4326', '{EQUAL_AREA_CRS}'
               ))) AS shared_area
        FROM "{name}_02_tmp1" c
        JOIN "{name}_02_tmp2" p
          ON ST_XMax(p.part_geom) >= ST_XMin(c.part_geom)
         AND ST_XMin(p.part_geom) <= ST_XMax(c.part_geom)
         AND ST_YMax(p.part_geom) >= ST_YMin(c.part_geom)
         AND ST_YMin(p.part_geom) <= ST_YMax(c.part_geom)
         AND ST_Intersects(c.part_geom, p.part_geom)
        GROUP BY c.fid, p.fid
    """)

    # Plurality pick per child, ties broken by lowest parent fid.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_assign" AS
        SELECT child_fid, parent_fid FROM (
            SELECT child_fid, parent_fid,
                   ROW_NUMBER() OVER (
                       PARTITION BY child_fid ORDER BY shared_area DESC, parent_fid ASC
                   ) AS rn
            FROM "{name}_02_pairs"
            WHERE shared_area > 0
        ) WHERE rn = 1
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_unassigned" AS
        SELECT fid AS child_fid FROM "{name}_child_01"
        WHERE fid NOT IN (SELECT child_fid FROM "{name}_02_assign")
    """)

    unassigned = conn.execute(
        f'SELECT child_fid FROM "{name}_02_unassigned" ORDER BY child_fid'
    ).fetchall()
    if unassigned:
        fids = [row[0] for row in unassigned]
        logger.warning(
            "match: dropping %d unmatched child fid(s) with no parent overlap: %s",
            len(fids),
            fids,
        )

    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp1"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp2"')
