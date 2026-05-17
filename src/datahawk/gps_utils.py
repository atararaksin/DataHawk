from __future__ import annotations

from datahawk.types import Line, Point

def create_perpendecular_line(point: Point, heading: float, length: float) -> Line:
    """For a trjectory passing through the `point` with `heading`,
    creates a perpendecular line of length `length` (in meters).
    """
    #TODO

def intersection(line: Line, a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> Point:
    """Returns intersection point of a-b line and the given line,
    or null if they don't intesect.
    """
    # TODO

def interpolate_by_gps(actual_lat: float, actual_lon: float,
                       a_lat: float, a_lon: float, a_val: float,
                       b_lat: float, b_lon: float, b_val: float):
    """Returns interpolated value that can be assumed in the actual location between points a and b.
    Here the caller must have ensured `actual` lies on the line between a and b.
    """
    # TODO