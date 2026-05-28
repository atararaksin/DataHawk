"""Lap detection from XRZ channel 4 data + GPS S/F line crossing."""

from __future__ import annotations

from datahawk.source.types import SourceSession
from datahawk.source.channel_constants import GPS_LATITUDE, GPS_LONGITUDE, GPS_HEADING, MASTER_CLK, BEACON
from datahawk.types import Line, Point
from datahawk.gps_utils import create_perpendecular_line, intersection, interpolate_by_gps, mad_average_of_lines
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


def detect_start_finish_fine(session: SourceSession) -> Line:
    """Detect start/finish line coordinates from ch4 markers + GPS heading."""
    start_finish_times = get_sf_timestamps_based_on_ch4(session)
    if not start_finish_times:
        raise ValueError("Couldn't detect start/finish line from ch4")

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


def detect_laps(session: SourceSession, sf_line: Line) -> list[float]:
    """Detect lap boundary times by finding GPS crossings of the S/F line.

    Returns list of crossing times (Master Clk values at each S/F crossing).
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

    return crossings
