"""Session utility functions for cross-lap/cross-session lookups."""

from __future__ import annotations

import math

from datahawk.types import Session, Lap


def get_sample_index_for_session_time(session: Session, session_time: float) -> int:
    """Get the reindexed sample index for a given session time using the temporal index."""
    start = session.laps[0].lap_start_time
    idx = int((session_time - start) / session.time_resolution)
    if idx < 0:
        return 0
    if idx >= len(session.temporal_index):
        return session.temporal_index[-1].sample_index if session.temporal_index else 0
    return session.temporal_index[idx].sample_index


def get_lap_idx_by_session_time(session: Session, session_time: float) -> int:
    """Find the active lap index for a given session time."""
    lap_idx = 0
    for i, lap in enumerate(session.laps):
        if session_time >= lap.lap_start_time:
            lap_idx = i
        else:
            break
    return lap_idx


def get_channel_value_in_another_lap_with_interpolation(
    source_session: Session, source_session_time: float, target_lap: Lap, channel_name: str
) -> float:
    """Look up a channel value in target_lap at the spatial position corresponding to source_session_time.

    Uses source_session's temporal index to find sample_idx, then interpolates using
    actual Master Clk values of the source lap (not temporal index timestamps) for precision.
    Returns NaN if data is unavailable.
    """
    start = source_session.laps[0].lap_start_time
    res = source_session.time_resolution
    idx = int((source_session_time - start) / res)

    if idx < 0 or idx >= len(source_session.temporal_index):
        return float('nan')

    entry = source_session.temporal_index[idx]
    sample_idx = entry.sample_index
    source_lap = source_session.laps[entry.lap_index]

    # Get actual Master Clk values at sample_idx and sample_idx+1 in source lap
    source_mc = source_lap.master_clk
    if not source_mc or sample_idx + 1 >= len(source_mc.samples):
        return float('nan')

    source_mc1 = source_mc.samples[sample_idx]
    source_mc2 = source_mc.samples[sample_idx + 1]
    if math.isnan(source_mc1) or math.isnan(source_mc2):
        return float('nan')

    # Fraction of source_session_time between the two actual Master Clk values
    denom = source_mc2 - source_mc1
    if abs(denom) < 1e-12:
        frac = 0.0
    else:
        frac = (source_session_time - source_mc1) / denom
        frac = max(0.0, min(1.0, frac))

    # Get target channel values at same spatial positions
    ch = target_lap.channels.get(channel_name)
    if not ch or sample_idx + 1 >= len(ch.samples):
        return float('nan')

    val1 = ch.samples[sample_idx]
    val2 = ch.samples[sample_idx + 1]
    if math.isnan(val1) or math.isnan(val2):
        return float('nan')

    return val1 + frac * (val2 - val1)


def create_perpendicular_line_at_time(session: Session, session_time: float):
    """Create a perpendicular line at the given session time using the track's master lap.
    
    Returns the Line or None if invalid.
    """
    from datahawk.types import Point
    from datahawk.utils.gps_utils import create_perpendecular_line
    from datahawk.constants import CROSSING_LINE_LENGTH

    sample_idx = get_sample_index_for_session_time(session, session_time)

    # Check if within track limits using Master Clk continuity on current lap
    current_lap = session.laps[get_lap_idx_by_session_time(session, session_time)]
    mc_ch = current_lap.master_clk
    if mc_ch and sample_idx + 1 < len(mc_ch.samples):
        if math.isnan(mc_ch.samples[sample_idx]) or math.isnan(mc_ch.samples[sample_idx + 1]):
            return None

    # Get master lap's lat/lon/heading at this spatial position
    master_lap = session.track.master_lap
    if sample_idx >= len(master_lap.lats):
        return None

    lat = master_lap.lats[sample_idx]
    lon = master_lap.lons[sample_idx]
    heading = master_lap.headings[sample_idx]

    if math.isnan(lat) or math.isnan(lon) or math.isnan(heading):
        return None

    return create_perpendecular_line(Point(lat, lon), heading, CROSSING_LINE_LENGTH)
