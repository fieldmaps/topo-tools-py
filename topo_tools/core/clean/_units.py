"""Meters <-> degrees conversion for CLI-facing distance/area thresholds.

All data is normalized to EPSG:4326 (degrees) by inputs.py, so ST_CoverageClean
and ST_CoverageInvalidEdges_Agg's distance parameters are in degrees. Users
reason in meters, so CLI flags are meters and we convert here using a
latitude-aware factor: one degree of longitude shrinks by cos(latitude), so
we scale by the dataset's centroid latitude. Approximate over very large
north-south extents -- adequate for a cleaning tolerance. Ported from
topo-tools-js/src/lib/tools/topology-cleaner/pipeline/units.ts.
"""

from math import cos, radians

METERS_PER_DEGREE = 111_320


def cos_lat_factor(centroid_lat: float) -> float:
    """Latitude-scale factor, guarded near the poles so it never collapses to ~0."""
    return max(cos(radians(centroid_lat)), 0.05)


def meters_to_degrees(meters: float, centroid_lat: float) -> float:
    """Convert a distance in meters to degrees at the given centroid latitude."""
    return meters / (METERS_PER_DEGREE * cos_lat_factor(centroid_lat))


def deg_sq_to_m2(area_deg_sq: float, centroid_lat: float) -> float:
    """Convert an area in square degrees to square meters at the given latitude."""
    return area_deg_sq * METERS_PER_DEGREE**2 * cos_lat_factor(centroid_lat)


def m2_to_deg_sq(area_m2: float, centroid_lat: float) -> float:
    """Convert an area in square meters to square degrees at the given latitude."""
    return area_m2 / (METERS_PER_DEGREE**2 * cos_lat_factor(centroid_lat))


def deg_to_m(deg: float) -> float:
    """Convert a scalar degree distance (e.g. MIC radius) to meters.

    Uses the latitude-scale constant only (no cos(lat) factor) -- exact for
    N-S distances, approximate for E-W, adequate for display-only widths.
    Matches units.ts's degToM.
    """
    return deg * METERS_PER_DEGREE
