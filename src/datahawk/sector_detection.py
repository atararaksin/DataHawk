"""Sector detection from GPS crossing of sector split lines."""

from __future__ import annotations

import math

from datahawk.types import Session, Lap, Line
from datahawk.gps_utils import intersection, interpolate_by_gps
from datahawk.session_utils import get_channel_value_in_another_lap_with_interpolation


def detect_reference_lap_sector_split_times(session: Session) -> list[float]:
    """Detect times at which the reference lap crosses each sector split line.

    Returns crossing times ordered by time of crossing.
    If no split lines, returns empty list.
    """
    split_lines = session.track.sector_split_lines
    if not split_lines:
        return []

    ref_lap = session.laps[session.reference_lap_index]
    lat_ch = ref_lap.channels.get("GPS Latitude")
    lon_ch = ref_lap.channels.get("GPS Longitude")
    mc_ch = ref_lap.channels.get("Master Clk")

    if not (lat_ch and lon_ch and mc_ch):
        return []

    # Use raw data for precise crossing detection
    lats = lat_ch.raw_values
    lons = lon_ch.raw_values
    mc_raw_ts = mc_ch.raw_timestamps
    mc_raw_vals = mc_ch.raw_values

    crossing_times: list[float] = []

    for line in split_lines:
        best_time = None
        for i in range(len(lats) - 1):
            pt = intersection(line, lats[i], lons[i], lats[i + 1], lons[i + 1])
            if pt is not None:
                # Interpolate Master Clk at crossing point
                t = interpolate_by_gps(
                    pt.lat, pt.lon,
                    lats[i], lons[i], mc_raw_vals[i],
                    lats[i + 1], lons[i + 1], mc_raw_vals[i + 1],
                )
                best_time = t
                break  # Take first crossing per line
        if best_time is not None:
            crossing_times.append(best_time)

    # Order by crossing time
    crossing_times.sort()
    return crossing_times


def calculate_sector_times(session: Session, reference_lap_sector_split_times: list[float], lap: Lap) -> list[float]:
    """Calculate sector durations for a lap based on reference lap sector split positions.

    Returns list of sector durations. Always one more sector than split times.
    NaN for sectors where split time couldn't be determined.
    """
    n_splits = len(reference_lap_sector_split_times)

    # Get split times for this lap by looking up Master Clk at corresponding positions
    lap_split_times: list[float] = []
    for ref_time in reference_lap_sector_split_times:
        t = get_channel_value_in_another_lap_with_interpolation(
            session, ref_time, lap, "Master Clk"
        )
        lap_split_times.append(t)

    # Build sector durations: n_splits + 1 sectors
    # Boundaries: [lap_start_time, split1, split2, ..., lap_start_time + lap_time]
    boundaries = [lap.lap_start_time] + lap_split_times + [lap.lap_start_time + lap.lap_time]

    sector_times: list[float] = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        if math.isnan(start) or math.isnan(end):
            sector_times.append(float('nan'))
        else:
            sector_times.append(end - start)

    return sector_times


def populate_sector_times(session: Session):
    """Populate sector_times for all laps in the session."""
    ref_split_times = detect_reference_lap_sector_split_times(session)

    for lap in session.laps:
        if not ref_split_times:
            lap.sector_times = [lap.lap_time]
        else:
            lap.sector_times = calculate_sector_times(session, ref_split_times, lap)
