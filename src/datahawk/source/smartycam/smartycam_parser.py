"""Parse AiM SmartyCam MP4 telemetry (aimd track) into a SourceSession.

SmartyCam embeds telemetry in an MP4 metadata track with handler 'MetaAimHandler'
and sample description type 'aimd'. The track runs at 10 Hz and contains:
- Channel values in tagged '(S' records (CAN bus data from MyChron + internal channels)
- GPS fixes in 'hGPS' sections with ECEF coordinates + velocity

Produces a SourceSession with GPS, acceleration, speed, and Master Clock channels.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

from datahawk.source.types import SourceChannel, SourceSession, SourceSessionMetadata
from datahawk.source.channel_constants import (
    GPS_LATITUDE, GPS_LONGITUDE, GPS_SPEED, GPS_HEADING, MASTER_CLK,
)
from datahawk.utils.mp4_utils import find_top_level_box


# WGS84 constants for ECEF conversion
_WGS84_A = 6_378_137.0
_WGS84_B = 6_356_752.314245
_WGS84_E2 = 1 - (_WGS84_B ** 2) / (_WGS84_A ** 2)


def is_smartycam_video(path: str | Path) -> bool:
    """Check if an MP4 file contains a SmartyCam aimd telemetry track."""
    path = Path(path)
    if not path.exists() or path.stat().st_size < 1000:
        return False
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        moov_offset = find_top_level_box(f, file_size, b"moov")
        if moov_offset < 0:
            return False
        f.seek(moov_offset)
        moov_size = struct.unpack(">I", f.read(4))[0]
        f.read(4)
        moov_data = f.read(min(moov_size - 8, 500_000))
        return b"MetaAimHandler" in moov_data and b"aimd" in moov_data

# Channel byte IDs from CAN bus (mapped from SmartyCam aimd config)
_CH_RPM = 0x20
_CH_SPEED = 0x21
_CH_GEAR = 0x22
_CH_WATER_TEMP = 0x23
_CH_BRAKE_PRESS = 0x28
_CH_TPS = 0x29
_CH_LATERAL_ACC = 0x2E
_CH_INLINE_ACC = 0x2F
_CH_VERTICAL_ACC = 0x32


def parse_smartycam(video_path: str | Path) -> SourceSession:
    """Parse telemetry from a SmartyCam MP4 into a SourceSession.

    Returns a SourceSession with GPS lat/lon/speed/heading and Master Clock channels.
    Video sync is deterministic (Master Clock shared via CAN), so video_offset = 0.
    """
    path = Path(video_path)
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()

        moov_offset = find_top_level_box(f, file_size, b"moov")
        if moov_offset < 0:
            raise ValueError("No moov box found")

        f.seek(moov_offset)
        moov_size = struct.unpack(">I", f.read(4))[0]
        f.read(4)
        moov_data = f.read(moov_size - 8)

        stco_data, stsz_data, sample_count, stsc_entries = _find_aimd_track(moov_data)
        if sample_count == 0:
            raise ValueError("No aimd telemetry track found")

        # Read all samples
        gps_fixes: list[tuple[float, float, float, float, float]] = []  # (ts_sec, lat, lon, speed_kmh, heading)
        channel_data: dict[int, list[tuple[float, float]]] = {}  # ch_id -> [(ts_sec, value)]

        sample_offsets = _compute_sample_offsets(stco_data, stsz_data, sample_count, stsc_entries)

        for i in range(sample_count):
            off = sample_offsets[i]
            sz = struct.unpack(">I", stsz_data[i * 4:i * 4 + 4])[0]
            f.seek(off)
            sample = f.read(sz)

            # Skip config sample (first sample, typically large with channel definitions)
            if i == 0 and sample.find(b"amv0") >= 0 and sample.find(b"hCHS") >= 0:
                continue

            _parse_sample(sample, gps_fixes, channel_data)

    if not gps_fixes:
        raise ValueError("No GPS data found in SmartyCam video")

    # Build SourceSession channels
    lat_ch = SourceChannel(name=GPS_LATITUDE)
    lon_ch = SourceChannel(name=GPS_LONGITUDE)
    speed_ch = SourceChannel(name=GPS_SPEED)
    heading_ch = SourceChannel(name=GPS_HEADING)
    mclk_ch = SourceChannel(name=MASTER_CLK)

    # Normalize timestamps: first GPS fix defines t=0
    t0 = gps_fixes[0][0]

    for ts, lat, lon, speed, heading in gps_fixes:
        t = ts - t0
        lat_ch.append(t, lat)
        lon_ch.append(t, lon)
        speed_ch.append(t, speed)
        heading_ch.append(t, heading)
        mclk_ch.append(t, ts)  # Master Clock = absolute time from power-on

    channels = {
        GPS_LATITUDE: lat_ch,
        GPS_LONGITUDE: lon_ch,
        GPS_SPEED: speed_ch,
        GPS_HEADING: heading_ch,
        MASTER_CLK: mclk_ch,
    }

    metadata = SourceSessionMetadata(
        track="",
        date="",
        time="",
        session_type="SmartyCam",
    )

    session = SourceSession(metadata=metadata, channels=channels)

    from datahawk.session_processing.synthetic_channels import add_synthetic_channels
    add_synthetic_channels(session)
    return session


def _find_aimd_track(moov_data: bytes) -> tuple[bytes, bytes, int, list[tuple[int, int]]]:
    """Find the aimd telemetry track in moov. Returns (stco_data, stsz_data, sample_count, stsc_entries)."""
    pos = 0
    while pos + 8 <= len(moov_data):
        size = struct.unpack(">I", moov_data[pos:pos + 4])[0]
        btype = moov_data[pos + 4:pos + 8]
        if size < 8:
            break
        if btype == b"trak":
            trak = moov_data[pos + 8:pos + size]
            # Check for MetaAimHandler
            if b"MetaAimHandler" in trak and b"aimd" in trak:
                stco_idx = trak.find(b"stco")
                stsz_idx = trak.find(b"stsz")
                stsc_idx = trak.find(b"stsc")
                if stco_idx < 0 or stsz_idx < 0:
                    break

                # stco
                count = struct.unpack(">I", trak[stco_idx + 8:stco_idx + 12])[0]
                stco_data = trak[stco_idx + 12:stco_idx + 12 + count * 4]

                # stsz
                sample_count = struct.unpack(">I", trak[stsz_idx + 16:stsz_idx + 20])[0]
                stsz_data = trak[stsz_idx + 20:stsz_idx + 20 + sample_count * 4]

                # stsc (sample-to-chunk mapping)
                stsc_entries = []
                if stsc_idx >= 0:
                    stsc_count = struct.unpack(">I", trak[stsc_idx + 8:stsc_idx + 12])[0]
                    for i in range(stsc_count):
                        fc = struct.unpack(">I", trak[stsc_idx + 12 + i * 12:stsc_idx + 16 + i * 12])[0]
                        spc = struct.unpack(">I", trak[stsc_idx + 16 + i * 12:stsc_idx + 20 + i * 12])[0]
                        stsc_entries.append((fc, spc))

                return stco_data, stsz_data, sample_count, stsc_entries
        pos += size
    return b"", b"", 0, []


def _compute_sample_offsets(stco_data: bytes, stsz_data: bytes, sample_count: int,
                            stsc_entries: list[tuple[int, int]]) -> list[int]:
    """Compute file offset for each sample using stco + stsc + stsz."""
    chunk_count = len(stco_data) // 4
    chunk_offsets = [struct.unpack(">I", stco_data[i * 4:i * 4 + 4])[0] for i in range(chunk_count)]

    # Build per-chunk samples_per_chunk from stsc
    # stsc entries: [(first_chunk_1based, samples_per_chunk), ...]
    spc_map = []  # (start_chunk_0based, samples_per_chunk)
    for fc, spc in stsc_entries:
        spc_map.append((fc - 1, spc))

    offsets = []
    chunk_idx = 0
    offset_in_chunk = 0
    sample_idx_in_chunk = 0

    # Determine samples_per_chunk for current chunk
    def get_spc(cidx):
        result = stsc_entries[0][1] if stsc_entries else 3
        for fc, spc in stsc_entries:
            if cidx >= fc - 1:
                result = spc
            else:
                break
        return result

    current_off = chunk_offsets[0] if chunk_offsets else 0
    for i in range(sample_count):
        spc = get_spc(chunk_idx)
        if sample_idx_in_chunk >= spc:
            chunk_idx += 1
            if chunk_idx < chunk_count:
                current_off = chunk_offsets[chunk_idx]
            sample_idx_in_chunk = 0

        offsets.append(current_off)
        sz = struct.unpack(">I", stsz_data[i * 4:i * 4 + 4])[0]
        current_off += sz
        sample_idx_in_chunk += 1

    return offsets


def _parse_sample(sample: bytes, gps_fixes: list, channel_data: dict) -> None:
    """Parse a single aimd sample, extracting GPS fixes and channel values."""
    # Parse (S records for channel values
    _parse_channel_records(sample, channel_data)

    # Parse GPS section
    gps_idx = sample.find(b"hGPS")
    if gps_idx >= 0:
        _parse_gps_section(sample, gps_idx, gps_fixes)


def _parse_channel_records(sample: bytes, channel_data: dict) -> None:
    """Parse '(S' tagged channel value records from a sample.

    Record format (13 bytes): 28 53 [ts_le_u16] [2 pad] [ch_id] [1 pad] [value_le_f32] [29]
    """
    idx = 0
    while idx + 13 <= len(sample):
        p = sample.find(b"\x28\x53", idx)
        if p < 0 or p + 13 > len(sample):
            break
        if sample[p + 12] != 0x29:  # closing ')' marker
            idx = p + 2
            continue

        ts_cs = struct.unpack("<H", sample[p + 2:p + 4])[0]
        ch_id = sample[p + 6]
        value = struct.unpack("<f", sample[p + 8:p + 12])[0]

        if not math.isnan(value) and value != -2.0:  # -2.0 and NaN are sentinel values
            ts_sec = ts_cs / 1000.0  # milliseconds from power-on
            if ch_id not in channel_data:
                channel_data[ch_id] = []
            channel_data[ch_id].append((ts_sec, value))

        idx = p + 13


def _parse_gps_section(sample: bytes, gps_idx: int, gps_fixes: list) -> None:
    """Parse GPS fix from hGPS section. ECEF coordinates in centimeters.

    GPS payload (56 bytes at hGPS+11):
      [0:4]   timestamp (LE u32, centiseconds from power-on)
      [4:8]   secondary timestamp
      [8:12]  altitude-related field
      [12:16] constant reference field
      [16:20] ECEF X (LE i32, centimeters)
      [20:24] ECEF Y (LE i32, centimeters)
      [24:28] ECEF Z (LE i32, centimeters)
      [28:32] altitude (meters?)
      [32:36] velocity X (LE i32, cm/s)
      [36:40] velocity Y (LE i32, cm/s)
      [40:44] velocity Z (LE i32, cm/s)
      [44:48] nsat + flags
      [48:56] reserved
    """
    payload_start = gps_idx + 11  # 4 (tag) + 7 (header)
    if payload_start + 56 > len(sample):
        return

    payload = sample[payload_start:payload_start + 56]

    ts_cs = struct.unpack("<I", payload[0:4])[0]
    ts_sec = ts_cs / 1000.0  # milliseconds from power-on

    ecef_x = struct.unpack("<i", payload[16:20])[0] / 100.0  # cm -> m
    ecef_y = struct.unpack("<i", payload[20:24])[0] / 100.0
    ecef_z = struct.unpack("<i", payload[24:28])[0] / 100.0

    # Velocity for speed and heading
    vx = struct.unpack("<i", payload[32:36])[0] / 100.0  # cm/s -> m/s
    vy = struct.unpack("<i", payload[36:40])[0] / 100.0
    vz = struct.unpack("<i", payload[40:44])[0] / 100.0

    speed_ms = math.sqrt(vx * vx + vy * vy + vz * vz)
    speed_kmh = speed_ms * 3.6

    # Convert ECEF to lat/lon
    lat, lon = _ecef_to_geodetic(ecef_x, ecef_y, ecef_z)

    # Skip invalid fixes
    if abs(lat) < 1.0 or abs(lon) < 1.0:
        return

    # Heading from horizontal velocity components (approximate from ECEF velocity)
    # Project velocity to local ENU frame for heading
    if speed_ms < 1.0:
        heading = float('nan')
    else:
        heading = _ecef_velocity_to_heading(ecef_x, ecef_y, ecef_z, vx, vy, vz)

    gps_fixes.append((ts_sec, lat, lon, speed_kmh, heading))


def _ecef_to_geodetic(x_m: float, y_m: float, z_m: float) -> tuple[float, float]:
    """Convert ECEF (meters) to geodetic lat/lon (degrees) using Bowring's method."""
    lon = math.degrees(math.atan2(y_m, x_m))
    p = math.hypot(x_m, y_m)
    theta = math.atan2(z_m * _WGS84_A, p * _WGS84_B)
    lat = math.degrees(math.atan2(
        z_m + _WGS84_E2 * _WGS84_A ** 2 / _WGS84_B * math.sin(theta) ** 3,
        p - _WGS84_E2 * _WGS84_A * math.cos(theta) ** 3,
    ))
    return lat, lon


def _ecef_velocity_to_heading(x: float, y: float, z: float,
                              vx: float, vy: float, vz: float) -> float:
    """Convert ECEF velocity to heading (degrees, 0=North, clockwise).

    Projects ECEF velocity into local East-North-Up frame at the given position.
    """
    # Compute local frame unit vectors at position
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p)

    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)

    # East unit vector
    e_e = (-sin_lon, cos_lon, 0)
    # North unit vector
    e_n = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)

    # Project velocity onto east/north
    v_east = vx * e_e[0] + vy * e_e[1] + vz * e_e[2]
    v_north = vx * e_n[0] + vy * e_n[1] + vz * e_n[2]

    heading = math.degrees(math.atan2(v_east, v_north)) % 360
    return heading
