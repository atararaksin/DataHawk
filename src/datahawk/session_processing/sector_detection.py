"""Sector detection from GPS crossing of sector split lines."""

from __future__ import annotations

import math

from datahawk.types import Session, Lap, Line
from datahawk.source.channel_constants import MASTER_CLK
from datahawk.utils.gps_utils import intersection, interpolate_by_gps
from datahawk.session_utils import get_channel_value_in_another_lap_with_interpolation


def detect_master_lap_sector_split_times(session: Session) -> list[float]:
    """Detect times at which the master lap crosses each sector split line.

    Returns crossing times ordered by time of crossing.
    Also reorders session.track.sector_split_lines to match chronological order.
    If no split lines, returns empty list.
    """
    split_lines = session.track.sector_split_lines
    if not split_lines:
        return []

    master_lap = session.track.master_lap
    lats = master_lap.lats
    lons = master_lap.lons

    # Use reference lap's Master Clk for time interpolation
    ref_lap = session.laps[session.best_lap_index]
    mc_ch = ref_lap.master_clk
    if not mc_ch:
        return []
    mcs = mc_ch.samples

    # Pair each line with its crossing time
    line_time_pairs: list[tuple[Line, float]] = []

    for line in split_lines:
        best_time = None
        for i in range(len(lats) - 1):
            if math.isnan(lats[i]) or math.isnan(lats[i + 1]):
                continue
            if math.isnan(lons[i]) or math.isnan(lons[i + 1]):
                continue
            pt = intersection(line, lats[i], lons[i], lats[i + 1], lons[i + 1])
            if pt is not None:
                if i >= len(mcs) - 1 or math.isnan(mcs[i]) or math.isnan(mcs[i + 1]):
                    continue
                # Interpolate Master Clk at crossing point
                t = interpolate_by_gps(
                    pt.lat, pt.lon,
                    lats[i], lons[i], mcs[i],
                    lats[i + 1], lons[i + 1], mcs[i + 1],
                )
                best_time = t
                break  # Take first crossing per line
        if best_time is not None:
            line_time_pairs.append((line, best_time))

    # Sort by crossing time and reorder track's split lines
    line_time_pairs.sort(key=lambda x: x[1])
    session.track.sector_split_lines = [pair[0] for pair in line_time_pairs]
    return [pair[1] for pair in line_time_pairs]


def calculate_sector_split_times(session: Session, reference_lap_sector_split_times: list[float], lap: Lap) -> list[float]:
    """Get absolute sector split times for a lap by looking up Master Clk at reference positions.

    Returns list of split times (same length as reference_lap_sector_split_times). NaN if off-track.
    """
    lap_split_times: list[float] = []
    for ref_time in reference_lap_sector_split_times:
        t = get_channel_value_in_another_lap_with_interpolation(
            session, ref_time, lap, MASTER_CLK
        )
        lap_split_times.append(t)
    return lap_split_times


def calculate_sector_times(reference_lap_sector_split_times: list[float], lap: Lap) -> list[float]:
    """Calculate sector durations for a lap from its sector_split_times.

    Returns list of sector durations. Always one more sector than split times.
    NaN for sectors where split time couldn't be determined.
    """
    # Boundaries: [lap_start_time, split1, split2, ..., lap_start_time + lap_time]
    boundaries = [lap.lap_start_time] + list(lap.sector_split_times) + [lap.lap_start_time + lap.lap_time]

    sector_times: list[float] = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        if math.isnan(start) or math.isnan(end):
            sector_times.append(float('nan'))
        else:
            sector_times.append(end - start)

    return sector_times


def populate_sectors(session: Session):
    """Populate sector_split_times and sector_times for all laps in the session."""
    ref_split_times = detect_master_lap_sector_split_times(session)

    for lap in session.laps:
        if not ref_split_times:
            lap.sector_split_times = []
            lap.sector_times = [lap.lap_time]
        else:
            lap.sector_split_times = calculate_sector_split_times(session, ref_split_times, lap)
            lap.sector_times = calculate_sector_times(ref_split_times, lap)
