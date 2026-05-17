"""XRZ file parser for AiM MyChron 5 telemetry data."""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union


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


@dataclass
class XrzChannel:
    """A telemetry channel definition."""
    id: int
    short_name: str
    long_name: str
    is_float16: bool = True
    timestamps: list[float] = field(default_factory=list, repr=False)
    values: list[float] = field(default_factory=list, repr=False)

    @property
    def name(self) -> str:
        return self.long_name or self.short_name

    def append(self, ts: float, val: float) -> None:
        self.timestamps.append(ts)
        self.values.append(val)

    def get_value_at_time_with_interpolation(self, ts: float) -> float:
        """Interpolate channel value at given timestamp using binary search."""
        import bisect
        i = bisect.bisect_right(self.timestamps, ts)
        if i == 0:
            return self.values[0]
        if i >= len(self.timestamps):
            return self.values[-1]
        t0, t1 = self.timestamps[i - 1], self.timestamps[i]
        frac = (ts - t0) / (t1 - t0) if t1 != t0 else 0.0
        return self.values[i - 1] + frac * (self.values[i] - self.values[i - 1])


@dataclass
class XrzSessionMetadata:
    """Non-temporal session metadata."""
    track: str = ""
    date: str = ""
    time: str = ""
    session_type: str = ""


@dataclass
class XrzSession:
    """Complete parsed XRZ session."""
    metadata: XrzSessionMetadata
    channels: dict[int, XrzChannel]


# CHS block header pattern: <hCHS\x00 + len=112(4) + flags=1(1) + >(1)
_CHS_PATTERN = bytes.fromhex("3c684348530070000000013e")
_CHS_BODY_LEN = 112


def _parse_channels(dec: bytes) -> dict[int, XrzChannel]:
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
        channels[seq] = XrzChannel(id=seq, short_name=short, long_name=long_name, is_float16=is_float16)
        pos = p + 12 + _CHS_BODY_LEN
        seq += 1
    return channels


def _parse_metadata(dec: bytes) -> XrzSessionMetadata:
    """Extract session metadata from header blocks."""
    import re
    meta = XrzSessionMetadata()

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


def _parse_frames(dec: bytes, channels: dict[int, XrzChannel]) -> None:
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
_GPS_LATACC_ID = -7
_GPS_LONACC_ID = -8
_GPS_DIST_ID = -9
_GPS_HEADING_ID = -10


def _parse_gps_blocks(dec: bytes, channels: dict[int, XrzChannel]) -> None:
    """Parse GPS blocks and add lat/lon/speed as synthetic channels."""
    lat_ch = XrzChannel(id=_GPS_LAT_ID, short_name="GPSLat", long_name="GPS Latitude")
    lon_ch = XrzChannel(id=_GPS_LON_ID, short_name="GPSLon", long_name="GPS Longitude")
    speed_ch = XrzChannel(id=_GPS_SPEED_ID, short_name="GPSSpd", long_name="GPS Speed")
    vn_ch = XrzChannel(id=_GPS_VN_ID, short_name="GPSvN", long_name="GPS Velocity N")
    ve_ch = XrzChannel(id=_GPS_VE_ID, short_name="GPSvE", long_name="GPS Velocity E")
    vd_ch = XrzChannel(id=_GPS_VD_ID, short_name="GPSvD", long_name="GPS Velocity D")
    heading_ch = XrzChannel(id=_GPS_HEADING_ID, short_name="GPSHdg", long_name="GPS Heading")

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

    # Heading from position deltas (vN/vE are NOT true N/E components)
    _HEADING_GAP = 5
    _M_PER_DEG_LAT = 111320.0
    _m_per_deg_lon = 111320.0 * math.cos(math.radians(lat_ch.values[0])) if lat_ch.values else 1.0
    for i in range(_HEADING_GAP, len(lat_ch.timestamps)):
        speed_at_i = speed_ch.values[i] if i < len(speed_ch.values) else 0
        if speed_at_i < 2.5:
            continue
        dn = (lat_ch.values[i] - lat_ch.values[i - _HEADING_GAP]) * _M_PER_DEG_LAT
        de = (lon_ch.values[i] - lon_ch.values[i - _HEADING_GAP]) * _m_per_deg_lon
        if abs(dn) < 0.05 and abs(de) < 0.05:
            continue
        heading_ch.append(lat_ch.timestamps[i], math.degrees(math.atan2(de, dn)) % 360)

    if lat_ch.timestamps:
        channels[_GPS_LAT_ID] = lat_ch
        channels[_GPS_LON_ID] = lon_ch
    if speed_ch.timestamps:
        channels[_GPS_SPEED_ID] = speed_ch
        channels[_GPS_VN_ID] = vn_ch
        channels[_GPS_VE_ID] = ve_ch
        channels[_GPS_VD_ID] = vd_ch
        channels[_GPS_HEADING_ID] = heading_ch

        # Compute lateral and longitudinal acceleration from speed + heading
        _compute_gps_acceleration(speed_ch, heading_ch, channels)

        # Compute cumulative distance from lat/lon
        _compute_gps_distance(lat_ch, lon_ch, channels)


def _compute_gps_acceleration(speed_ch: XrzChannel, heading_ch: XrzChannel,
                              channels: dict[int, XrzChannel]) -> None:
    """Compute GPS lateral/longitudinal acceleration from speed and heading."""
    lat_acc = XrzChannel(id=_GPS_LATACC_ID, short_name="GPSLatG", long_name="GPS Lat Acc")
    lon_acc = XrzChannel(id=_GPS_LONACC_ID, short_name="GPSLonG", long_name="GPS Lon Acc")

    N = 5  # smoothing half-window
    for i in range(N, len(speed_ch.timestamps) - N):
        t_i = speed_ch.timestamps[i]
        dt = speed_ch.timestamps[i + N] - speed_ch.timestamps[i - N]
        if dt <= 0:
            continue

        # Longitudinal = dSpeed/dt
        ds = (speed_ch.values[i + N] - speed_ch.values[i - N]) / 3.6
        lon_g = (ds / dt) / 9.81

        # Lateral = speed * dHeading/dt (heading from position channel)
        h0 = heading_ch.get_value_at_time_with_interpolation(speed_ch.timestamps[i - N])
        h1 = heading_ch.get_value_at_time_with_interpolation(speed_ch.timestamps[i + N])
        dh = math.radians(h1) - math.radians(h0)
        if dh > math.pi: dh -= 2 * math.pi
        if dh < -math.pi: dh += 2 * math.pi

        spd_ms = speed_ch.values[i] / 3.6
        lat_g = (spd_ms * dh / dt) / 9.81

        lat_acc.append(t_i, lat_g)
        lon_acc.append(t_i, lon_g)

    if lat_acc.timestamps:
        channels[_GPS_LATACC_ID] = lat_acc
        channels[_GPS_LONACC_ID] = lon_acc


def _compute_gps_distance(lat_ch: XrzChannel, lon_ch: XrzChannel,
                           channels: dict[int, XrzChannel]) -> None:
    """Compute cumulative GPS distance in meters from session start."""
    dist_ch = XrzChannel(id=_GPS_DIST_ID, short_name="GPSDist", long_name="GPS Distance")
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
    channels[_GPS_DIST_ID] = dist_ch


def parse_xrz(path: Union[Path, str]) -> XrzSession:
    """Parse an XRZ file and return structured telemetry data."""
    raw = Path(path).read_bytes()
    dec = zlib.decompress(raw)

    channels = _parse_channels(dec)
    metadata = _parse_metadata(dec)
    _parse_frames(dec, channels)
    _parse_gps_blocks(dec, channels)

    return XrzSession(metadata=metadata, channels=channels)
