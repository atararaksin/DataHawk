"""Generic MP4 container parsing utilities."""

from __future__ import annotations

import datetime
import struct
from pathlib import Path


def find_top_level_box(f, file_size: int, box_type: bytes) -> int:
    """Find a top-level MP4 box by type. Returns offset or -1."""
    f.seek(0)
    while f.tell() < file_size:
        pos = f.tell()
        header = f.read(8)
        if len(header) < 8:
            break
        size = struct.unpack(">I", header[:4])[0]
        btype = header[4:8]
        if size == 0:
            size = file_size - pos
        elif size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
        if btype == box_type:
            return pos
        f.seek(pos + size)
    return -1


def get_mp4_creation_time(path: Path) -> datetime.datetime | None:
    """Extract creation time from MP4 mvhd box. Returns UTC datetime or None."""
    mp4_epoch = datetime.datetime(1904, 1, 1, tzinfo=datetime.timezone.utc)

    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()

        moov_offset = find_top_level_box(f, file_size, b"moov")
        if moov_offset < 0:
            return None

        f.seek(moov_offset)
        moov_size = struct.unpack(">I", f.read(4))[0]
        f.read(4)  # skip 'moov'

        # Find mvhd within moov
        mvhd_data = f.read(min(moov_size - 8, 200))
        mvhd_idx = mvhd_data.find(b"mvhd")
        if mvhd_idx < 0:
            return None

        version = mvhd_data[mvhd_idx + 4]
        if version == 0:
            creation_secs = struct.unpack(">I", mvhd_data[mvhd_idx + 8:mvhd_idx + 12])[0]
        else:
            creation_secs = struct.unpack(">Q", mvhd_data[mvhd_idx + 8:mvhd_idx + 16])[0]

        if creation_secs == 0:
            return None

        return mp4_epoch + datetime.timedelta(seconds=creation_secs)
