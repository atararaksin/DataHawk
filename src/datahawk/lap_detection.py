"""Lap time detection from XRZ channel 4 data."""

from __future__ import annotations

import struct
from datahawk.xrz_parser import ParsedSession


def detect_laps(parsed: ParsedSession) -> list[float]:
    """Detect lap times from a parsed XRZ session.
    
    Returns list of lap durations in seconds.
    Uses channel 4 (Predictive Time): a lap boundary event is one where
    the reported value (ms) matches elapsed time since last boundary (±500ms).
    """
    boundaries = detect_lap_boundaries(parsed)
    return [boundaries[i+1] - boundaries[i] for i in range(len(boundaries) - 1)]


def detect_lap_boundaries(parsed: ParsedSession) -> list[float]:
    """Detect lap boundary timestamps from channel 4.
    
    Returns list of timestamps (seconds) marking each S/F crossing.
    """
    ch4 = parsed.channels.get(4)
    if not ch4 or not ch4.timestamps:
        return []

    # Reinterpret float32 -> uint32 (channel 4 stores lap time as uint32 ms)
    events = []
    for i in range(len(ch4.timestamps)):
        t = ch4.timestamps[i]
        fval = ch4.values[i]
        raw = struct.unpack('<I', struct.pack('<f', fval))[0]
        events.append((t, raw))

    # Valid lap time events (45-90s)
    valid_events = [(t, v) for t, v in events if 45000 <= v <= 90000]
    if not valid_events:
        return []

    # Find longest chain where value ≈ elapsed time since last boundary
    best_chain = []
    for start_idx in range(min(len(valid_events), 50)):
        ts, val = valid_events[start_idx]
        chain = [ts - val / 1000.0, ts]
        current_end_ms = ts * 1000

        for j in range(start_idx + 1, len(valid_events)):
            ts_j, val_j = valid_events[j]
            ts_j_ms = ts_j * 1000
            elapsed = ts_j_ms - current_end_ms
            if abs(val_j - elapsed) < 500:
                chain.append(ts_j)
                current_end_ms = ts_j_ms

        if len(chain) > len(best_chain):
            best_chain = chain

    return best_chain


def best_lap_time(parsed: ParsedSession) -> float | None:
    """Return the best (fastest) lap time in seconds, or None if no laps detected."""
    laps = detect_laps(parsed)
    return min(laps) if laps else None
