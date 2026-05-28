from __future__ import annotations

import math
from datahawk.types import Line, Point

# Approximate meters per degree at mid-latitudes
_M_PER_DEG_LAT = 111_320.0


def _m_per_deg_lon(lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat))


def create_perpendecular_line(point: Point, heading: float, length: float) -> Line:
    """For a trajectory passing through `point` with `heading` (degrees, 0=N),
    creates a perpendicular line of `length` meters centered on point.
    """
    # Perpendicular heading is heading + 90
    perp_rad = math.radians(heading + 90)
    half = length / 2.0
    dlat = math.cos(perp_rad) * half / _M_PER_DEG_LAT
    dlon = math.sin(perp_rad) * half / _m_per_deg_lon(point.lat)
    return Line(
        a=Point(point.lat - dlat, point.lon - dlon),
        b=Point(point.lat + dlat, point.lon + dlon),
    )


def intersection(line: Line, a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> Point | None:
    """Returns intersection point of segment a->b with `line`, or None if no intersection."""
    # Line segment intersection using parametric form
    x1, y1 = line.a.lon, line.a.lat
    x2, y2 = line.b.lon, line.b.lat
    x3, y3 = a_lon, a_lat
    x4, y4 = b_lon, b_lat

    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-15:
        return None

    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom

    if 0 <= t <= 1 and 0 <= u <= 1:
        ix = x1 + t * (x2 - x1)
        iy = y1 + t * (y2 - y1)
        return Point(lat=iy, lon=ix)
    return None


def interpolate_by_gps(actual_lat: float, actual_lon: float,
                       a_lat: float, a_lon: float, a_val: float,
                       b_lat: float, b_lon: float, b_val: float) -> float:
    """Interpolate value at `actual` position between points a and b."""
    # Use distance fraction along a->b
    da = math.hypot((actual_lat - a_lat) * _M_PER_DEG_LAT,
                    (actual_lon - a_lon) * _m_per_deg_lon(a_lat))
    db = math.hypot((b_lat - a_lat) * _M_PER_DEG_LAT,
                    (b_lon - a_lon) * _m_per_deg_lon(a_lat))
    if db < 1e-9:
        return a_val
    frac = da / db
    return a_val + frac * (b_val - a_val)


def _dist_m(a: Point, b: Point) -> float:
    """Distance in meters between two points."""
    return math.hypot((a.lat - b.lat) * _M_PER_DEG_LAT,
                      (a.lon - b.lon) * _m_per_deg_lon(a.lat))


def mad_average_of_lines(lines: list[Line]) -> Line:
    """Average lines after removing outliers using MAD (Median Absolute Deviation).

    Computes midpoints, finds median, measures distances from median,
    then uses MAD-based adaptive threshold to filter outliers.
    """
    if not lines:
        raise ValueError("No lines to average")
    if len(lines) == 1:
        return lines[0]

    mids = [Point((l.a.lat + l.b.lat) / 2, (l.a.lon + l.b.lon) / 2) for l in lines]
    lats = sorted(m.lat for m in mids)
    lons = sorted(m.lon for m in mids)
    median_pt = Point(lats[len(lats) // 2], lons[len(lons) // 2])

    dists = [_dist_m(m, median_pt) for m in mids]
    med_dist = sorted(dists)[len(dists) // 2]
    mad = sorted(abs(d - med_dist) for d in dists)[len(dists) // 2]
    threshold = med_dist + 3 * 1.4826 * mad

    filtered = [l for l, d in zip(lines, dists) if d < threshold]
    if not filtered:
        filtered = lines  # fallback: use all

    avg_a_lat = sum(l.a.lat for l in filtered) / len(filtered)
    avg_a_lon = sum(l.a.lon for l in filtered) / len(filtered)
    avg_b_lat = sum(l.b.lat for l in filtered) / len(filtered)
    avg_b_lon = sum(l.b.lon for l in filtered) / len(filtered)
    return Line(a=Point(avg_a_lat, avg_a_lon), b=Point(avg_b_lat, avg_b_lon))
