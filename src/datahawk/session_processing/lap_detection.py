"""Lap detection: S/F line detection and GPS crossing detection."""

from __future__ import annotations

import math

from datahawk.source.types import SourceSession, SourceChannel
from datahawk.source.channel_constants import BEACON
from datahawk.types import Line, Point
from datahawk.utils.gps_utils import create_perpendecular_line, intersection, interpolate_by_gps, mad_average_of_lines
from datahawk.constants import CROSSING_LINE_LENGTH


def get_sf_timestamps_based_on_ch4(session: SourceSession) -> list[float]:
    """Find S/F crossing timestamps from ch4 duplicate-value pairs."""
    ch4 = session.channels.get(BEACON)
    if not ch4 or len(ch4.timestamps) < 4:
        return []

    times = []
    i = 0
    while i < len(ch4.values) - 1:
        # Duplicate consecutive values (same ms) = S/F crossing marker
        if ch4.values[i] == ch4.values[i + 1]:
            times.append(ch4.timestamps[i])
            i += 2  # skip the pair
        else:
            i += 1
    return times


def detect_sf_from_mychron_beacon(session: SourceSession, beacon_ch: SourceChannel) -> Line:
    """Detect start/finish line coordinates from MyChron beacon timestamps + GPS heading."""
    start_finish_times = get_sf_timestamps_based_on_ch4(session)
    if not start_finish_times:
        raise ValueError("Couldn't detect start/finish line from beacon")

    lat_ch = session.gps_lat
    lon_ch = session.gps_lon
    heading_ch = session.gps_heading

    lines: list[Line] = []
    for t in start_finish_times:
        lat = lat_ch.get_value_at_time_with_interpolation(t)
        lon = lon_ch.get_value_at_time_with_interpolation(t)
        heading = heading_ch.get_value_at_time_with_interpolation(t)
        lines.append(create_perpendecular_line(Point(lat, lon), heading, CROSSING_LINE_LENGTH))

    if not lines:
        raise ValueError("Couldn't detect start/finish line")

    return mad_average_of_lines(lines)


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


def detect_laps(session: SourceSession, sf_line: Line) -> list[float]:
    """Detect lap boundaries by finding GPS crossings of the S/F line.

    Returns full boundary list: [session_start, crossing1, crossing2, ..., session_end].
    """
    lat_ch = session.gps_lat
    lon_ch = session.gps_lon
    mclk_ch = session.master_clk
    if not lat_ch or not lon_ch or not mclk_ch:
        return []

    crossings: list[float] = []
    last_crossing_time = -10.0  # minimum gap between crossings

    for i in range(len(lat_ch.timestamps) - 1):
        t = lat_ch.timestamps[i]
        if t - last_crossing_time < 10.0:
            continue

        pt = intersection(
            sf_line,
            lat_ch.values[i], lon_ch.values[i],
            lat_ch.values[i + 1], lon_ch.values[i + 1],
        )
        if pt is not None:
            # Interpolate Master Clk at the crossing point
            crossing_time = interpolate_by_gps(
                pt.lat, pt.lon,
                lat_ch.values[i], lon_ch.values[i], mclk_ch.get_value_at_time_with_interpolation(lat_ch.timestamps[i]),
                lat_ch.values[i + 1], lon_ch.values[i + 1], mclk_ch.get_value_at_time_with_interpolation(lat_ch.timestamps[i + 1]),
            )
            crossings.append(crossing_time)
            last_crossing_time = t

    if not crossings:
        return []

    # Build full boundary list: session_start, crossings..., session_end
    from datahawk.source.channel_constants import MASTER_CLK as _MCLK
    mclk = session.channels.get(_MCLK)
    session_start_time = mclk.timestamps[0] if mclk and mclk.timestamps else crossings[0]
    session_end_time = mclk.timestamps[-1] if mclk and mclk.timestamps else crossings[-1]

    return [session_start_time] + crossings + [session_end_time]
