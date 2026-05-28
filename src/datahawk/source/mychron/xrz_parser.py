"""XRZ file parser for AiM MyChron 5 telemetry data."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import Union

from datahawk.source.types import SourceChannel, SourceSession, SourceSessionMetadata
from datahawk.source.channel_constants import (
    GPS_LATITUDE, GPS_LONGITUDE, GPS_SPEED,
    GPS_VELOCITY_1, GPS_VELOCITY_2, GPS_VELOCITY_3,
    MASTER_CLK, BEACON,
)


# WGS84 ellipsoid constants
_WGS84_A = 6_378_137.0  # semi-major axis (m)
_WGS84_B = 6_356_752.314245  # semi-minor axis (m)
_WGS84_E2 = 1 - (_WGS84_B ** 2) / (_WGS84_A ** 2)  # first eccentricity squared


def _ecef_to_geodetic(x_m: float, y_m: float, z_m: float) -> tuple[float, float]:
    """Convert ECEF (meters) to geodetic lat/lon (degrees) using Bowring's method."""
    lon = math.degrees(math.atan2(y_m, x_m))
    p = math.hypot(x_m, y_m)
    # Initial estimate of latitude (Bowring)
    theta = math.atan2(z_m * _WGS84_A, p * _WGS84_B)
    lat = math.atan2(
        z_m + _WGS84_E2 * _WGS84_A ** 2 / _WGS84_B * math.sin(theta) ** 3,
        p - _WGS84_E2 * _WGS84_A * math.cos(theta) ** 3,
    )
    lat = math.degrees(lat)
    return lat, lon



# Internal parsing helper -- tracks is_float16 which SourceChannel doesn't need
from dataclasses import dataclass, field

@dataclass
class _ParseChannel:
    """Internal channel used during XRZ parsing (tracks encoding info)."""
    name: str
    is_float16: bool = True
    timestamps: list[float] = field(default_factory=list, repr=False)
    values: list[float] = field(default_factory=list, repr=False)

    def append(self, ts: float, val: float) -> None:
        self.timestamps.append(ts)
        self.values.append(val)

    def get_value_at_time_with_interpolation(self, ts: float) -> float:
        import bisect
        i = bisect.bisect_right(self.timestamps, ts)
        if i == 0:
            return self.values[0]
        if i >= len(self.timestamps):
            return self.values[-1]
        t0, t1 = self.timestamps[i - 1], self.timestamps[i]
        frac = (ts - t0) / (t1 - t0) if t1 != t0 else 0.0
        return self.values[i - 1] + frac * (self.values[i] - self.values[i - 1])

    def to_source_channel(self) -> SourceChannel:
        ch = SourceChannel(name=self.name)
        ch.timestamps = self.timestamps
        ch.values = self.values
        return ch


# CHS block header pattern: <hCHS\x00 + len=112(4) + flags=1(1) + >(1)
_CHS_PATTERN = bytes.fromhex("3c684348530070000000013e")
_CHS_BODY_LEN = 112

# Well-known MyChron channel indices -> canonical names
_KNOWN_CHANNEL_NAMES = {
    0: MASTER_CLK,
    4: BEACON,
}


def _parse_channels(dec: bytes) -> dict[int, _ParseChannel]:
    """Extract channel definitions from CHS blocks. Key = sequential index (frame ID)."""
    channels = {}
    pos = 0
    seq = 0
    while True:
        p = dec.find(_CHS_PATTERN, pos)
        if p == -1:
            break
        body = dec[p + 12: p + 12 + _CHS_BODY_LEN]
        short = body[24:32].split(b"\x00")[0].decode("ascii", errors="replace")
        long_name = body[32:64].split(b"\x00")[0].decode("ascii", errors="replace")
        # b16=1 + b20=20 -> float16; b16=2 -> raw uint16
        b16 = struct.unpack_from("<H", body, 16)[0]
        is_float16 = (b16 != 2)
        name = _KNOWN_CHANNEL_NAMES.get(seq, long_name or short)
        channels[seq] = _ParseChannel(name=name, is_float16=is_float16)
        pos = p + 12 + _CHS_BODY_LEN
        seq += 1
    return channels


def _parse_metadata(dec: bytes) -> SourceSessionMetadata:
    """Extract session metadata from header blocks."""
    import re
    meta = SourceSessionMetadata()

    # Track code from TRK block
    idx = dec.find(b"<hTRK ")
    if idx != -1:
        chunk = dec[idx + 12: idx + 12 + 96]
        code = chunk[:32].split(b"\x00")[0].decode("ascii", errors="replace")
        if code:
            meta.track = code

    # Date from TMD block
    idx = dec.find(b"<hTMD")
    if idx != -1:
        chunk = dec[idx: idx + 100]
        m = re.search(rb"(\d{2}/\d{2}/\d{4})", chunk)
        if m:
            meta.date = m.group(1).decode()

    # Time from TMT block
    idx = dec.find(b"<hTMT")
    if idx != -1:
        chunk = dec[idx: idx + 100]
        m = re.search(rb"(\d{2}:\d{2}:\d{2})", chunk)
        if m:
            meta.time = m.group(1).decode()

    # Session type
    if b"Best Lap of Test." in dec:
        meta.session_type = "Practice"

    return meta


def _parse_frames(dec: bytes, channels: dict[int, _ParseChannel]) -> None:
    """Parse (S frames and populate channel samples."""
    pos = 0
    end = len(dec)
    while pos < end - 10:
        idx = dec.find(b"\x28\x53", pos)
        if idx == -1:
            break

        if idx + 10 < end and dec[idx + 10] == 0x29:
            frame_len = 11
            val_size = 2
        elif idx + 12 < end and dec[idx + 12] == 0x29:
            frame_len = 13
            val_size = 4
        else:
            pos = idx + 2
            continue

        ts_raw = struct.unpack_from("<I", dec, idx + 2)[0]
        ch_id = struct.unpack_from("<H", dec, idx + 6)[0]
        ts_sec = ts_raw / 1000.0

        if val_size == 2:
            raw = struct.unpack_from("<H", dec, idx + 8)[0]
            if raw == 31744:  # float16 infinity = no data
                pos = idx + frame_len
                continue
            if ch_id in channels and channels[ch_id].is_float16:
                value = struct.unpack("<e", struct.pack("<H", raw))[0]
            else:
                value = float(raw)
        else:
            raw_u32 = struct.unpack_from("<I", dec, idx + 8)[0]
            value = struct.unpack_from("<f", dec, idx + 8)[0]
            # Channel 0 (Master Clk) stores uint32 milliseconds, not float32
            if ch_id == 0:
                value = raw_u32 / 1000.0
            elif value != value:  # NaN check
                pos = idx + frame_len
                continue

        if ch_id in channels:
            channels[ch_id].append(ts_sec, value)

        pos = idx + frame_len


_GPS_LAT_ID = -1  # synthetic channel IDs for GPS
_GPS_LON_ID = -2
_GPS_SPEED_ID = -3
_GPS_VN_ID = -4
_GPS_VE_ID = -5
_GPS_VD_ID = -6


def _parse_gps_blocks(dec: bytes, channels: dict[int, _ParseChannel]) -> None:
    """Parse GPS blocks and add lat/lon/speed as synthetic channels."""
    lat_ch = _ParseChannel(name=GPS_LATITUDE)
    lon_ch = _ParseChannel(name=GPS_LONGITUDE)
    speed_ch = _ParseChannel(name=GPS_SPEED)
    vn_ch = _ParseChannel(name=GPS_VELOCITY_1)
    ve_ch = _ParseChannel(name=GPS_VELOCITY_2)
    vd_ch = _ParseChannel(name=GPS_VELOCITY_3)

    # Encoding (confirmed ECEF Earth-Centered Earth-Fixed):
    # offset 0: timestamp (ms, session-local)
    # offset 16: ECEF X (centimeters, signed int32)
    # offset 20: ECEF Y (centimeters, signed int32)
    # offset 24: ECEF Z (centimeters, signed int32)
    # offset 32: velocity component 1 (cm/s, signed)
    # offset 36: velocity component 2 (cm/s, signed)
    # offset 40: velocity component 3 (cm/s, signed)

    pos = 0
    while True:
        idx = dec.find(b"<hGPS\x00", pos)
        if idx == -1:
            break
        bs = idx + 12
        if bs + 40 > len(dec):
            break

        ts_sec = struct.unpack_from("<I", dec, bs)[0] / 1000.0
        ecef_x = struct.unpack_from("<i", dec, bs + 16)[0] / 100.0  # cm -> m
        ecef_y = struct.unpack_from("<i", dec, bs + 20)[0] / 100.0
        ecef_z = struct.unpack_from("<i", dec, bs + 24)[0] / 100.0

        pos = idx + 12

        lat, lon = _ecef_to_geodetic(ecef_x, ecef_y, ecef_z)

        if abs(lat) < 1.0 or abs(lon) < 1.0:
            continue

        lat_ch.append(ts_sec, lat)
        lon_ch.append(ts_sec, lon)

        # Speed from velocity components (confirmed 3D matches Race Studio)
        vn = struct.unpack_from("<i", dec, bs + 32)[0]
        ve = struct.unpack_from("<i", dec, bs + 36)[0]
        vd = struct.unpack_from("<i", dec, bs + 40)[0]
        if abs(vn) < 50000 and abs(ve) < 50000:
            speed_kmh = math.sqrt(vn**2 + ve**2 + vd**2) * 3.6 / 100
            speed_ch.append(ts_sec, speed_kmh)
            vn_ch.append(ts_sec, vn * 3.6 / 100)
            ve_ch.append(ts_sec, ve * 3.6 / 100)
            vd_ch.append(ts_sec, vd * 3.6 / 100)

    if lat_ch.timestamps:
        channels[_GPS_LAT_ID] = lat_ch
        channels[_GPS_LON_ID] = lon_ch
    if speed_ch.timestamps:
        channels[_GPS_SPEED_ID] = speed_ch
        channels[_GPS_VN_ID] = vn_ch
        channels[_GPS_VE_ID] = ve_ch
        channels[_GPS_VD_ID] = vd_ch




def parse_xrz(path: Union[Path, str]) -> SourceSession:
    """Parse an XRZ file and return structured telemetry data."""
    raw = Path(path).read_bytes()
    dec = zlib.decompress(raw)

    channels = _parse_channels(dec)
    metadata = _parse_metadata(dec)
    _parse_frames(dec, channels)
    _parse_gps_blocks(dec, channels)

    # Convert int-keyed internal channels to name-keyed SourceChannels
    result: dict[str, SourceChannel] = {}
    for ch in channels.values():
        result[ch.name] = ch.to_source_channel()
    return SourceSession(metadata=metadata, channels=result)
