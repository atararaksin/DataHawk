"""Lap time detection from XRZ channel 4 data."""

from __future__ import annotations

import struct
from datahawk.xrz_parser import XrzSession
from datahawk.types import Line, Point
from datahawk.gps_utils import create_perpendecular_line

_CROSSING_LINE_LENGTH = 8.0 #in meters

def detect_start_finish_fine(session: XrzSession) -> Line:
    """Detect start finish line coordinates.
    Uses channel 4 (Predictive Time): a lap boundary event where duplicated
    consecutive values signify an actual lap time upon SF crossing.
    """
    start_finish_times: list[float] = get_sf_timestamps_based_on_ch4(session)
    if (len(start_finish_times) == 0):
        raise Error("Couldn't detect start/finish line")

    lat_ch = session.channels.get(-1)
    lon_ch = session.channels.get(-2)
    heading_ch = session.channels.get( #TODO - can you parse GPS Heading from XRZ?

    lines = []
    for start_finish_time in start_finish_times:
        lat = lat_ch.get_value_at_time_with_interpolation(start_finish_time)
        lon = lon_ch.get_value_at_time_with_interpolation(start_finish_time)
        heading = heading_ch.get_value_at_time_with_interpolation(start_finish_time)
        line = create_perpendecular_line(Point(lat, lon), heading, _CROSSING_LINE_LENGTH)
        lines.append(line)

    if (Len(lines) == 0):
        raise Error("Couldn't detect start/finish line")

    sf_line = #TODO - throw away outliers from `lines` and take an average of the rest
    return sf_line


def detect_laps(session: XrzSession, sf_line: Line) -> list[float]:
    """Detect lap times from a parsed XRZ session based on S/F line crossing.
    
    Returns list of lap durations in seconds.
    """
    # TODO
    # Iterate over session datapoints until found a consecutive pair of points that cross sf_line
    # use gps_utils.intersection() to get intesection
    # once found intersection, use gps_utils.interpolate_by_gps() to get interpolated Master Clk value at intersection
    # Now you have lap boundary time.
    # Build array of lap durations