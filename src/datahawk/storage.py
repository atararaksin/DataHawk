"""Persistent storage: SQLite metadata + session files."""

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from platformdirs import user_data_dir

from datahawk.types import Track, Line, Point

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
    sf_line TEXT NOT NULL,
    sector_split_lines TEXT NOT NULL DEFAULT '[]'
);
"""


def _get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
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


def _line_to_json(line: Line) -> list:
    return [line.a.lat, line.a.lon, line.b.lat, line.b.lon]


def _line_from_json(arr: list) -> Line:
    return Line(Point(arr[0], arr[1]), Point(arr[2], arr[3]))


def save_track(track: Track):
    """Save/update track SF line and sector split lines."""
    db = _get_db()
    sf = json.dumps(_line_to_json(track.sf_line))
    splits = json.dumps([_line_to_json(l) for l in track.sector_split_lines])
    db.execute(
        "INSERT OR REPLACE INTO tracks (name, sf_line, sector_split_lines) VALUES (?, ?, ?)",
        (track.name, sf, splits),
    )
    db.commit()
    db.close()


def load_track(name: str) -> Track | None:
    """Load track from DB. Returns None if not found."""
    db = _get_db()
    row = db.execute("SELECT sf_line, sector_split_lines FROM tracks WHERE name = ?", (name,)).fetchone()
    db.close()
    if not row:
        return None
    sf = _line_from_json(json.loads(row["sf_line"]))
    splits = [_line_from_json(arr) for arr in json.loads(row["sector_split_lines"])]
    return Track(name=name, sf_line=sf, sector_split_lines=splits)


def delete_track(name: str):
    """Remove track from DB."""
    db = _get_db()
    db.execute("DELETE FROM tracks WHERE name = ?", (name,))
    db.commit()
    db.close()
