"""Persistent storage: SQLite metadata + session files."""

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "DataHawk"
DATA_DIR = Path(user_data_dir(APP_NAME, appauthor=False))
SESSIONS_DIR = DATA_DIR / "sessions"
DB_PATH = DATA_DIR / "datahawk.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    driver TEXT NOT NULL DEFAULT '',
    date TEXT,
    time TEXT,
    laps TEXT,
    track TEXT,
    size INTEGER,
    best_lap_time REAL,
    file_path TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    name TEXT PRIMARY KEY,
    sector_split_lines TEXT NOT NULL DEFAULT '[]',
    sf_line TEXT,
    master_lap_lats TEXT,
    master_lap_lons TEXT
);
"""


def _get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Migration: wipe tracks table (schema changed, single user)
    # TODO: remove this after running once locally
    conn.execute("DELETE FROM tracks")
    conn.commit()
    return conn


def get_imported_filenames() -> set[str]:
    """Return set of original_filename values already imported."""
    db = _get_db()
    rows = db.execute("SELECT original_filename FROM sessions").fetchall()
    db.close()
    return {r["original_filename"] for r in rows}


def save_session(driver: str, original_filename: str, data: bytes,
                 date: str = "", time: str = "", laps: str = "",
                 track: str = "", best_lap_time: float = None) -> str:
    """Save session file and metadata. Overwrites if already imported. Returns session ID."""
    db = _get_db()

    existing = db.execute(
        "SELECT id, file_path FROM sessions WHERE original_filename = ?",
        (original_filename,)
    ).fetchone()

    if existing:
        (SESSIONS_DIR / existing["file_path"]).write_bytes(data)
        db.execute(
            """UPDATE sessions SET driver=?, date=?, time=?, laps=?, track=?,
               size=?, best_lap_time=?, imported_at=? WHERE id=?""",
            (driver, date, time, laps, track, len(data), best_lap_time,
             datetime.now().isoformat(), existing["id"]),
        )
        db.commit()
        db.close()
        return existing["id"]

    session_id = uuid.uuid4().hex[:12]
    rel_path = f"{session_id}.xrz"
    (SESSIONS_DIR / rel_path).write_bytes(data)

    db.execute(
        """INSERT INTO sessions
           (id, driver, original_filename, date, time, laps, track, size, best_lap_time, file_path, imported_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, driver, original_filename, date, time, laps, track,
         len(data), best_lap_time, rel_path, datetime.now().isoformat()),
    )
    db.commit()
    db.close()
    return session_id


def list_saved_sessions() -> list[dict]:
    """Return all imported sessions."""
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM sessions ORDER BY date DESC, time DESC"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_session_file_path(session_id: str) -> Path | None:
    """Return absolute path to session file."""
    db = _get_db()
    row = db.execute("SELECT file_path FROM sessions WHERE id = ?", (session_id,)).fetchone()
    db.close()
    if row:
        return SESSIONS_DIR / row["file_path"]
    return None


def save_track_sectors(track_name: str, sector_split_lines: list) -> None:
    """Save sector split line coordinates for a track."""
    from datahawk.types import Line
    data = [[l.a.lat, l.a.lon, l.b.lat, l.b.lon] for l in sector_split_lines]
    db = _get_db()
    db.execute(
        "INSERT INTO tracks (name, sector_split_lines) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET sector_split_lines = ?",
        (track_name, json.dumps(data), json.dumps(data)),
    )
    db.commit()
    db.close()


def load_track_sectors(track_name: str) -> list | None:
    """Load saved sector split lines for a track. Returns list of Line or None."""
    from datahawk.types import Line, Point
    db = _get_db()
    row = db.execute("SELECT sector_split_lines FROM tracks WHERE name = ?", (track_name,)).fetchone()
    db.close()
    if not row:
        return None
    data = json.loads(row["sector_split_lines"])
    return [Line(Point(c[0], c[1]), Point(c[2], c[3])) for c in data]


def save_track_sf_line(track_name: str, sf_line) -> None:
    """Save a custom SF line for a track."""
    data = [sf_line.a.lat, sf_line.a.lon, sf_line.b.lat, sf_line.b.lon]
    db = _get_db()
    db.execute(
        "INSERT INTO tracks (name, sf_line) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET sf_line = ?",
        (track_name, json.dumps(data), json.dumps(data)),
    )
    db.commit()
    db.close()


def load_track_sf_line(track_name: str):
    """Load saved SF line for a track. Returns Line or None."""
    from datahawk.types import Line, Point
    db = _get_db()
    row = db.execute("SELECT sf_line FROM tracks WHERE name = ?", (track_name,)).fetchone()
    db.close()
    if not row or not row["sf_line"]:
        return None
    c = json.loads(row["sf_line"])
    return Line(Point(c[0], c[1]), Point(c[2], c[3]))


def delete_track(track_name: str) -> None:
    """Delete all saved track data (SF line + sectors)."""
    db = _get_db()
    db.execute("DELETE FROM tracks WHERE name = ?", (track_name,))
    db.commit()
    db.close()


def save_track(track) -> None:
    """Save a complete Track object to the database."""
    from datahawk.types import Line
    sectors_data = [[l.a.lat, l.a.lon, l.b.lat, l.b.lon] for l in track.sector_split_lines]
    sf_data = [track.sf_line.a.lat, track.sf_line.a.lon, track.sf_line.b.lat, track.sf_line.b.lon]
    db = _get_db()
    db.execute(
        """INSERT INTO tracks (name, sector_split_lines, sf_line, master_lap_lats, master_lap_lons)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             sector_split_lines = ?, sf_line = ?, master_lap_lats = ?, master_lap_lons = ?""",
        (track.name, json.dumps(sectors_data), json.dumps(sf_data),
         json.dumps(track.master_lap.lats), json.dumps(track.master_lap.lons),
         json.dumps(sectors_data), json.dumps(sf_data),
         json.dumps(track.master_lap.lats), json.dumps(track.master_lap.lons)),
    )
    db.commit()
    db.close()


def load_track(track_name: str):
    """Load a complete Track from the database. Returns Track or None."""
    from datahawk.types import Track, Line, Point, MasterLap
    db = _get_db()
    row = db.execute("SELECT * FROM tracks WHERE name = ?", (track_name,)).fetchone()
    db.close()
    if not row or not row["sf_line"] or not row["master_lap_lats"]:
        return None
    sf = json.loads(row["sf_line"])
    sectors_data = json.loads(row["sector_split_lines"])
    lats = json.loads(row["master_lap_lats"])
    lons = json.loads(row["master_lap_lons"])
    return Track(
        name=track_name,
        sf_line=Line(Point(sf[0], sf[1]), Point(sf[2], sf[3])),
        master_lap=MasterLap(lats=lats, lons=lons),
        sector_split_lines=[Line(Point(c[0], c[1]), Point(c[2], c[3])) for c in sectors_data],
    )


def list_tracks() -> list[str]:
    """Return all saved track names."""
    db = _get_db()
    rows = db.execute("SELECT name FROM tracks ORDER BY name").fetchall()
    db.close()
    return [r["name"] for r in rows]
