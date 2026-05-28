"""Compute synthetic channels derived from raw GPS data.

Called after parsing, before session processing. Adds channels that are
source-agnostic (same computation regardless of MyChron or GoPro).
"""

from __future__ import annotations

import math

from datahawk.source.types import SourceSession, SourceChannel
from datahawk.source.channel_constants import (
    GPS_LATITUDE, GPS_LONGITUDE, GPS_SPEED, GPS_HEADING,
    GPS_LAT_ACC, GPS_LON_ACC, GPS_DISTANCE,
)
from datahawk.utils.gps_utils import compute_gps_acceleration


def add_synthetic_channels(session: SourceSession) -> None:
    """Add all synthetic channels to a parsed session."""
    _add_gps_heading(session)
    _add_gps_acceleration(session)
    _add_gps_distance(session)


def _add_gps_heading(session: SourceSession) -> None:
    """Add GPS Heading from position deltas (gap=5, threshold 2.5 km/h)."""
    lat_ch = session.channels.get(GPS_LATITUDE)
    lon_ch = session.channels.get(GPS_LONGITUDE)
    speed_ch = session.channels.get(GPS_SPEED)
    if not lat_ch or not lon_ch or not speed_ch or not lat_ch.values:
        return

    heading_ch = SourceChannel(name=GPS_HEADING)
    gap = 5
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat_ch.values[0]))

    for i in range(len(lat_ch.timestamps)):
        if i < gap or speed_ch.values[i] < 2.5:
            heading_ch.append(lat_ch.timestamps[i], float('nan'))
            continue
        dn = (lat_ch.values[i] - lat_ch.values[i - gap]) * m_per_deg_lat
        de = (lon_ch.values[i] - lon_ch.values[i - gap]) * m_per_deg_lon
        if abs(dn) < 0.05 and abs(de) < 0.05:
            heading_ch.append(lat_ch.timestamps[i], float('nan'))
            continue
        heading_ch.append(lat_ch.timestamps[i], math.degrees(math.atan2(de, dn)) % 360)

    if heading_ch.timestamps:
        session.channels[GPS_HEADING] = heading_ch


def _add_gps_acceleration(session: SourceSession) -> None:
    """Add GPS Lat Acc and GPS Lon Acc from speed + heading."""
    speed_ch = session.channels.get(GPS_SPEED)
    heading_ch = session.channels.get(GPS_HEADING)
    if not speed_ch or not heading_ch:
        return

    lat_pairs, lon_pairs = compute_gps_acceleration(speed_ch, heading_ch)
    if lat_pairs:
        lat_acc = SourceChannel(name=GPS_LAT_ACC)
        lon_acc = SourceChannel(name=GPS_LON_ACC)
        for t, v in lat_pairs:
            lat_acc.append(t, v)
        for t, v in lon_pairs:
            lon_acc.append(t, v)
        session.channels[GPS_LAT_ACC] = lat_acc
        session.channels[GPS_LON_ACC] = lon_acc


def _add_gps_distance(session: SourceSession) -> None:
    """Add cumulative GPS distance in meters from session start."""
    lat_ch = session.channels.get(GPS_LATITUDE)
    lon_ch = session.channels.get(GPS_LONGITUDE)
    if not lat_ch or not lon_ch or not lat_ch.values:
        return

    dist_ch = SourceChannel(name=GPS_DISTANCE)
    lats = lat_ch.values
    lons = lon_ch.values
    times = lat_ch.timestamps
    cos_lat = math.cos(math.radians(lats[0]))
    cum_dist = 0.0
    dist_ch.append(times[0], 0.0)
    for i in range(1, len(lats)):
        dlat = (lats[i] - lats[i - 1]) * 111000
        dlon = (lons[i] - lons[i - 1]) * 111000 * cos_lat
        cum_dist += math.sqrt(dlat ** 2 + dlon ** 2)
        dist_ch.append(times[i], cum_dist)
    session.channels[GPS_DISTANCE] = dist_ch
