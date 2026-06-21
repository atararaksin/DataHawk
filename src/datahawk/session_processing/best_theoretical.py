"""Best theoretical lap: combine best sectors from all laps."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

from datahawk.source.types import SourceSession, SourceChannel
from datahawk.source.channel_constants import GPS_LATITUDE, GPS_LONGITUDE, GPS_DISTANCE, MASTER_CLK
from datahawk.session_processing.session_processing import reindex_lap
from datahawk.session_processing.synthetic_channels import add_gps_distance, add_lap_level_synthetic_channels
from datahawk.types import Lap, Track


def build_best_theoretical_lap(
    source_session: SourceSession,
    track: Track,
    laps: list[Lap],
) -> Lap:
    """Build a best theoretical lap by combining the best sector from each lap.

    Finds the fastest sector for each sector index across all laps,
    stitches raw source data from those sectors, reindexes against the master lap,
    and returns a complete Lap with index -1.
    """
    n_sectors = len(laps[0].sector_times) if laps and laps[0].sector_times else 1

    # Find best sector index for each sector
    best_sector_lap_idx = []
    best_sector_durations = []
    for s in range(n_sectors):
        best_time = float('inf')
        best_lap = 0
        for i, lap in enumerate(laps):
            if s < len(lap.sector_times) and not math.isnan(lap.sector_times[s]):
                if lap.sector_times[s] < best_time:
                    best_time = lap.sector_times[s]
                    best_lap = i
        best_sector_lap_idx.append(best_lap)
        best_sector_durations.append(best_time if best_time != float('inf') else 0.0)

    # Build session-time boundaries for each best sector
    # Each sector boundary: [sector_start_session_time, sector_end_session_time, source_lap]
    sector_ranges: list[tuple[float, float, Lap]] = []
    for s in range(n_sectors):
        lap = laps[best_sector_lap_idx[s]]
        # Sector boundaries in session time
        boundaries = [lap.lap_start_time] + list(lap.sector_split_times) + [lap.lap_start_time + lap.lap_time]
        sector_start = boundaries[s]
        sector_end = boundaries[s + 1]
        sector_ranges.append((sector_start, sector_end, lap))

    # Build SourceChannels by stitching sectors from source_session
    channel_names = [name for name, ch in source_session.channels.items()
                     if ch.timestamps and name != GPS_DISTANCE]

    theoretical_channels: dict[str, SourceChannel] = {}
    for ch_name in channel_names:
        theoretical_channels[ch_name] = SourceChannel(name=ch_name)

    master_clk_offset = 0.0  # cumulative time at start of each sector in theoretical lap

    for s, (sect_start, sect_end, lap) in enumerate(sector_ranges):
        # Determine master clk offset for this sector
        # First datapoint's offset from sector start
        first_point_offset = None

        for ch_name in channel_names:
            src_ch = source_session.channels[ch_name]
            lo = bisect_left(src_ch.timestamps, sect_start)
            hi = bisect_left(src_ch.timestamps, sect_end)

            if ch_name == MASTER_CLK:
                # Build Master Clk from cumulative offset
                for i in range(lo, hi):
                    point_session_time = src_ch.timestamps[i]
                    offset_from_sector_start = point_session_time - sect_start
                    theoretical_time = master_clk_offset + offset_from_sector_start
                    theoretical_channels[ch_name].append(theoretical_time, theoretical_time)
            else:
                for i in range(lo, hi):
                    point_session_time = src_ch.timestamps[i]
                    offset_from_sector_start = point_session_time - sect_start
                    theoretical_time = master_clk_offset + offset_from_sector_start
                    theoretical_channels[ch_name].append(theoretical_time, src_ch.values[i])

        master_clk_offset += best_sector_durations[s]

    # Add GPS Distance from lat/lon channels
    lat_ch = theoretical_channels.get(GPS_LATITUDE)
    lon_ch = theoretical_channels.get(GPS_LONGITUDE)
    if lat_ch and lon_ch:
        dist_ch = add_gps_distance(lat_ch, lon_ch)
        theoretical_channels[GPS_DISTANCE] = dist_ch

    # Reindex against master lap
    lap_time = sum(best_sector_durations)
    reindexed_channels = reindex_lap(
        source_channels=theoretical_channels,
        lap_start_time=0.0,
        lap_end_time=lap_time,
        master_lap=track.master_lap,
    )

    # Build Lap object
    lap = Lap(lap_index=-1, lap_time=lap_time, lap_start_time=0.0)
    lap.channels = reindexed_channels

    # Set sector times and split times from best sector durations
    lap.sector_times = list(best_sector_durations)
    # sector_split_times = cumulative sum of sector durations (excluding last)
    cumulative = 0.0
    lap.sector_split_times = []
    for s in range(n_sectors - 1):
        cumulative += best_sector_durations[s]
        lap.sector_split_times.append(cumulative)

    add_lap_level_synthetic_channels(lap)

    return lap
