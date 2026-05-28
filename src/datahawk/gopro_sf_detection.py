"""Start/finish line detection for GoPro sessions (no ch4 beacon data).

Strategy: Find the point of maximum speed, go back 2 seconds, draw a
perpendicular line to the heading at that point. This approximates the
start/finish line on the main straight.
"""

from __future__ import annotations

import math

from datahawk.source.types import SourceSession
from datahawk.types import Line, Point
from datahawk.gps_utils import create_perpendecular_line
from datahawk.constants import CROSSING_LINE_LENGTH


def detect_sf_from_max_speed(session: SourceSession) -> Line:
    """Detect S/F line by finding max speed point, going back 2s, and drawing perpendicular.

    The idea: max speed is typically on the main straight. Going back 2 seconds
    places us near the start of the straight where the S/F line usually is.
    """
    speed_ch = session.gps_speed
    lat_ch = session.gps_lat
    lon_ch = session.gps_lon
    heading_ch = session.gps_heading

    # Find max speed index
    max_speed = -1.0
    max_idx = 0
    for i, v in enumerate(speed_ch.values):
        if v > max_speed:
            max_speed = v
            max_idx = i

    # Go back 2 seconds from max speed point
    max_time = speed_ch.timestamps[max_idx]
    target_time = max_time - 2.0

    # Find the sample closest to target_time
    sf_idx = 0
    for i, t in enumerate(speed_ch.timestamps):
        if t >= target_time:
            sf_idx = i
            break

    lat = lat_ch.values[sf_idx]
    lon = lon_ch.values[sf_idx]
    heading = heading_ch.values[sf_idx]

    # If heading is NaN at this point, search forward for valid heading
    while math.isnan(heading) and sf_idx < len(heading_ch.values) - 1:
        sf_idx += 1
        heading = heading_ch.values[sf_idx]
        lat = lat_ch.values[sf_idx]
        lon = lon_ch.values[sf_idx]

    if math.isnan(heading):
        raise ValueError("Could not find valid heading for S/F line placement")

    return create_perpendecular_line(Point(lat, lon), heading, CROSSING_LINE_LENGTH)
