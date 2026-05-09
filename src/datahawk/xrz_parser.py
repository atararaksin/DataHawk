"""XRZ file parser for AiM MyChron 5 telemetry data."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union


@dataclass
class Channel:
    """A telemetry channel definition."""
    id: int
    short_name: str
    long_name: str
    is_float16: bool = True
    samples: list[tuple[float, float]] = field(default_factory=list, repr=False)

    @property
    def name(self) -> str:
        return self.long_name or self.short_name

    @property
    def timestamps(self) -> list[float]:
        return [s[0] for s in self.samples]

    @property
    def values(self) -> list[float]:
        return [s[1] for s in self.samples]


@dataclass
class SessionMetadata:
    """Non-temporal session metadata."""
    track: str = ""
    date: str = ""
    time: str = ""
    session_type: str = ""


@dataclass
class ParsedSession:
    """Complete parsed XRZ session."""
    metadata: SessionMetadata
    channels: dict[int, Channel]


# CHS block header pattern: <hCHS\x00 + len=112(4) + flags=1(1) + >(1)
_CHS_PATTERN = bytes.fromhex("3c684348530070000000013e")
_CHS_BODY_LEN = 112


def _parse_channels(dec: bytes) -> dict[int, Channel]:
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
        channels[seq] = Channel(id=seq, short_name=short, long_name=long_name, is_float16=is_float16)
        pos = p + 12 + _CHS_BODY_LEN
        seq += 1
    return channels


def _parse_metadata(dec: bytes) -> SessionMetadata:
    """Extract session metadata from header blocks."""
    import re
    meta = SessionMetadata()

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


def _parse_frames(dec: bytes, channels: dict[int, Channel]) -> None:
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
            value = struct.unpack_from("<f", dec, idx + 8)[0]
            if value != value:  # NaN check
                pos = idx + frame_len
                continue

        if ch_id in channels:
            channels[ch_id].samples.append((ts_sec, value))

        pos = idx + frame_len


_GPS_LAT_ID = -1  # synthetic channel IDs for GPS
_GPS_LON_ID = -2
_GPS_SPEED_ID = -3
_GPS_VN_ID = -4
_GPS_VE_ID = -5
_GPS_VD_ID = -6


def _parse_gps_blocks(dec: bytes, channels: dict[int, Channel]) -> None:
    """Parse GPS blocks and add lat/lon/speed as synthetic channels."""
    import math

    lat_ch = Channel(id=_GPS_LAT_ID, short_name="GPSLat", long_name="GPS Latitude")
    lon_ch = Channel(id=_GPS_LON_ID, short_name="GPSLon", long_name="GPS Longitude")
    speed_ch = Channel(id=_GPS_SPEED_ID, short_name="GPSSpd", long_name="GPS Speed")
    vn_ch = Channel(id=_GPS_VN_ID, short_name="GPSvN", long_name="GPS Velocity N")
    ve_ch = Channel(id=_GPS_VE_ID, short_name="GPSvE", long_name="GPS Velocity E")
    vd_ch = Channel(id=_GPS_VD_ID, short_name="GPSvD", long_name="GPS Velocity D")

    # Encoding (empirically determined from WCKC Chilliwack BC):
    # offset 0: timestamp (ms, session-local)
    # offset 20: longitude (raw / 2905385 = degrees)
    # offset 24: latitude (raw / 9768000 = degrees)
    # offset 32: velocity N component (cm/s, signed)
    # offset 36: velocity E component (cm/s, signed)
    LON_FACTOR = 2905385.0
    LAT_FACTOR = 9768000.0

    pos = 0
    while True:
        idx = dec.find(b"<hGPS\x00", pos)
        if idx == -1:
            break
        bs = idx + 12
        if bs + 40 > len(dec):
            break

        ts_sec = struct.unpack_from("<I", dec, bs)[0] / 1000.0
        lon = struct.unpack_from("<i", dec, bs + 20)[0] / LON_FACTOR
        lat = struct.unpack_from("<i", dec, bs + 24)[0] / LAT_FACTOR

        pos = idx + 12

        if abs(lat) < 1.0 or abs(lon) < 1.0:
            continue

        lat_ch.samples.append((ts_sec, lat))
        lon_ch.samples.append((ts_sec, lon))

        # Speed from 3D velocity components (cm/s)
        vn = struct.unpack_from("<i", dec, bs + 32)[0]
        ve = struct.unpack_from("<i", dec, bs + 36)[0]
        vd = struct.unpack_from("<i", dec, bs + 40)[0]
        if abs(vn) < 50000 and abs(ve) < 50000:
            speed_kmh = math.sqrt(vn**2 + ve**2 + vd**2) * 3.6 / 100
            speed_ch.samples.append((ts_sec, speed_kmh))
            vn_ch.samples.append((ts_sec, vn * 3.6 / 100))
            ve_ch.samples.append((ts_sec, ve * 3.6 / 100))
            vd_ch.samples.append((ts_sec, vd * 3.6 / 100))

    if lat_ch.samples:
        channels[_GPS_LAT_ID] = lat_ch
        channels[_GPS_LON_ID] = lon_ch
    if speed_ch.samples:
        channels[_GPS_SPEED_ID] = speed_ch
        channels[_GPS_VN_ID] = vn_ch
        channels[_GPS_VE_ID] = ve_ch
        channels[_GPS_VD_ID] = vd_ch


def parse_xrz(path: Union[Path, str]) -> ParsedSession:
    """Parse an XRZ file and return structured telemetry data."""
    raw = Path(path).read_bytes()
    dec = zlib.decompress(raw)

    channels = _parse_channels(dec)
    metadata = _parse_metadata(dec)
    _parse_frames(dec, channels)
    _parse_gps_blocks(dec, channels)

    return ParsedSession(metadata=metadata, channels=channels)
