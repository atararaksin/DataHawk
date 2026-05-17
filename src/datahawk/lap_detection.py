"""Lap detection from XRZ channel 4 data + GPS S/F line crossing."""

from __future__ import annotations

from datahawk.xrz_parser import XrzSession, _GPS_LAT_ID, _GPS_LON_ID, _GPS_HEADING_ID
from datahawk.types import Line, Point
from datahawk.gps_utils import create_perpendecular_line, intersection, interpolate_by_gps

_CROSSING_LINE_LENGTH = 8.0  # meters


def get_sf_timestamps_based_on_ch4(session: XrzSession) -> list[float]:
    """Find S/F crossing timestamps from ch4 duplicate-value pairs."""
    ch4 = session.channels.get(4)
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


def detect_start_finish_fine(session: XrzSession) -> Line:
    """Detect start/finish line coordinates from ch4 markers + GPS heading."""
    start_finish_times = get_sf_timestamps_based_on_ch4(session)
    if not start_finish_times:
        raise ValueError("Couldn't detect start/finish line from ch4")

    lat_ch = session.channels.get(_GPS_LAT_ID)
    lon_ch = session.channels.get(_GPS_LON_ID)
    heading_ch = session.channels.get(_GPS_HEADING_ID)
    if not lat_ch or not lon_ch or not heading_ch:
        raise ValueError("Missing GPS channels for S/F detection")

    lines: list[Line] = []
    for t in start_finish_times:
        lat = lat_ch.get_value_at_time_with_interpolation(t)
        lon = lon_ch.get_value_at_time_with_interpolation(t)
        heading = heading_ch.get_value_at_time_with_interpolation(t)
        lines.append(create_perpendecular_line(Point(lat, lon), heading, _CROSSING_LINE_LENGTH))

    if not lines:
        raise ValueError("Couldn't detect start/finish line")

    # Throw away outliers: compute median lat/lon of midpoints, discard >3m from median
    import math
    mids = [Point((l.a.lat + l.b.lat) / 2, (l.a.lon + l.b.lon) / 2) for l in lines]
    lats = sorted(m.lat for m in mids)
    lons = sorted(m.lon for m in mids)
    med_lat = lats[len(lats) // 2]
    med_lon = lons[len(lons) // 2]

    filtered: list[Line] = []
    for line, mid in zip(lines, mids):
        dist_m = math.hypot((mid.lat - med_lat) * 111320, (mid.lon - med_lon) * 111320 * math.cos(math.radians(med_lat)))
        if dist_m < 3.0:
            filtered.append(line)

    if not filtered:
        filtered = lines  # fallback: use all

    # Average the filtered lines
    avg_a_lat = sum(l.a.lat for l in filtered) / len(filtered)
    avg_a_lon = sum(l.a.lon for l in filtered) / len(filtered)
    avg_b_lat = sum(l.b.lat for l in filtered) / len(filtered)
    avg_b_lon = sum(l.b.lon for l in filtered) / len(filtered)
    return Line(a=Point(avg_a_lat, avg_a_lon), b=Point(avg_b_lat, avg_b_lon))


def detect_laps(session: XrzSession, sf_line: Line) -> list[float]:
    """Detect lap boundary times by finding GPS crossings of the S/F line.

    Returns list of crossing times (Master Clk values at each S/F crossing).
    """
    lat_ch = session.channels.get(_GPS_LAT_ID)
    lon_ch = session.channels.get(_GPS_LON_ID)
    mclk_ch = session.channels.get(0)
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
