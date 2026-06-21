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
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    driver TEXT NOT NULL DEFAULT '',
    date TEXT,
    time TEXT,
    laps TEXT,
    track TEXT,
    size INTEGER,
    best_lap_time REAL,
    file_path TEXT NOT NULL,
    video_path TEXT NOT NULL DEFAULT '',
    video_offset REAL,
    imported_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id)
);
CREATE TABLE IF NOT EXISTS tracks (
    name TEXT PRIMARY KEY,
    sector_split_lines TEXT NOT NULL DEFAULT '[]',
    sf_line TEXT,
    master_lap_lats TEXT,
    master_lap_lons TEXT,
    master_lap_headings TEXT
);
"""


def _get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_add_events(conn)
    return conn


def _migrate_add_events(conn: sqlite3.Connection):
    """Temporary migration: add event_id column to existing sessions table, create Archive event."""
    # Check if event_id column exists
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "event_id" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN event_id TEXT NOT NULL DEFAULT ''")
    # Create Archive event and assign all orphan sessions to it
    existing = conn.execute("SELECT id FROM events WHERE name = 'Archive'").fetchone()
    if not existing:
        archive_id = uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO events (id, name, date) VALUES (?, ?, ?)",
                     (archive_id, "Archive", datetime.now().strftime("%Y-%m-%d")))
        conn.execute("UPDATE sessions SET event_id = ? WHERE event_id = ''", (archive_id,))
        conn.commit()


# --- Event CRUD ---

def create_event(name: str, date: str = "") -> str:
    """Create a new event. Returns event ID."""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    event_id = uuid.uuid4().hex[:12]
    db = _get_db()
    db.execute("INSERT INTO events (id, name, date) VALUES (?, ?, ?)", (event_id, name, date))
    db.commit()
    db.close()
    return event_id


def list_events() -> list[dict]:
    """Return all events, newest first."""
    db = _get_db()
    rows = db.execute("SELECT * FROM events ORDER BY date DESC, name").fetchall()
    db.close()
    return [dict(r) for r in rows]


def delete_event(event_id: str) -> None:
    """Delete an event and all its sessions."""
    db = _get_db()
    session_rows = db.execute("SELECT id FROM sessions WHERE event_id = ?", (event_id,)).fetchall()
    for row in session_rows:
        delete_session(row["id"])
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    db.commit()
    db.close()


def get_event_track(event_id: str) -> str | None:
    """Return the track name of any session in this event, or None."""
    db = _get_db()
    row = db.execute("SELECT track FROM sessions WHERE event_id = ? AND track != '' LIMIT 1",
                     (event_id,)).fetchone()
    db.close()
    return row["track"] if row else None


def list_sessions_for_event(event_id: str) -> list[dict]:
    """Return all sessions belonging to an event."""
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM sessions WHERE event_id = ? ORDER BY date DESC, time DESC",
        (event_id,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_imported_filenames() -> set[str]:
    """Return set of (filename, date, time) tuples already imported."""
    db = _get_db()
    rows = db.execute("SELECT filename, date, time FROM sessions").fetchall()
    db.close()
    return {(r["filename"], r["date"] or "", r["time"] or "") for r in rows}


def save_session(driver: str, filename: str, data: bytes,
                 date: str = "", time: str = "", laps: str = "",
                 track: str = "", best_lap_time: float = None,
                 source_type: str = "", extension: str = ".xrz",
                 event_id: str = "") -> str:
    """Save session file and metadata. Overwrites if already imported. Returns session ID."""
    db = _get_db()

    existing = db.execute(
        "SELECT id, file_path FROM sessions WHERE filename = ? AND date = ? AND time = ? AND event_id = ?",
        (filename, date, time, event_id)
    ).fetchone()

    if existing:
        (SESSIONS_DIR / existing["file_path"]).write_bytes(data)
        db.execute(
            """UPDATE sessions SET driver=?, date=?, time=?, laps=?, track=?,
               size=?, best_lap_time=?, source_type=?, event_id=?, imported_at=? WHERE id=?""",
            (driver, date, time, laps, track, len(data), best_lap_time,
             source_type, event_id, datetime.now().isoformat(), existing["id"]),
        )
        db.commit()
        db.close()
        return existing["id"]

    session_id = uuid.uuid4().hex[:12]
    rel_path = f"{session_id}{extension}"
    (SESSIONS_DIR / rel_path).write_bytes(data)

    db.execute(
        """INSERT INTO sessions
           (id, event_id, driver, filename, source_type, date, time, laps, track, size, best_lap_time, file_path, imported_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, event_id, driver, filename, source_type, date, time, laps, track,
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



def list_drivers() -> list[str]:
    """Return distinct driver names from sessions, sorted."""
    db = _get_db()
    rows = db.execute("SELECT DISTINCT driver FROM sessions WHERE driver != '' ORDER BY driver").fetchall()
    db.close()
    return [r["driver"] for r in rows]

def get_session_file_path(session_id: str) -> Path | None:
    """Return absolute path to session file."""
    db = _get_db()
    row = db.execute("SELECT file_path FROM sessions WHERE id = ?", (session_id,)).fetchone()
    db.close()
    if row:
        return SESSIONS_DIR / row["file_path"]
    return None


def get_session_track_name(session_id: str) -> str | None:
    """Return track name for a session."""
    db = _get_db()
    row = db.execute("SELECT track FROM sessions WHERE id = ?", (session_id,)).fetchone()
    db.close()
    if row:
        return row["track"] or None
    return None


def get_session_source_type(session_id: str) -> str:
    """Return source_type for a session."""
    db = _get_db()
    row = db.execute("SELECT source_type FROM sessions WHERE id = ?", (session_id,)).fetchone()
    db.close()
    return row["source_type"] if row else ""


def get_session_video_info(session_id: str) -> tuple[str, float | None]:
    """Return (video_path, video_offset) for a session."""
    db = _get_db()
    row = db.execute("SELECT video_path, video_offset FROM sessions WHERE id = ?", (session_id,)).fetchone()
    db.close()
    if row:
        return row["video_path"] or "", row["video_offset"]
    return "", None


def save_session_video(session_id: str, video_path: str, video_offset: float | None) -> None:
    """Persist video path and offset for a session."""
    db = _get_db()
    db.execute("UPDATE sessions SET video_path=?, video_offset=? WHERE id=?",
               (video_path, video_offset, session_id))
    db.commit()
    db.close()


def delete_session(session_id: str) -> None:
    """Delete a session from the database and its data file."""
    path = get_session_file_path(session_id)
    if path and path.exists():
        path.unlink()
    db = _get_db()
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
    db.close()


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
        """INSERT INTO tracks (name, sector_split_lines, sf_line, master_lap_lats, master_lap_lons, master_lap_headings)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
             sector_split_lines = ?, sf_line = ?, master_lap_lats = ?, master_lap_lons = ?, master_lap_headings = ?""",
        (track.name, json.dumps(sectors_data), json.dumps(sf_data),
         json.dumps(track.master_lap.lats), json.dumps(track.master_lap.lons), json.dumps(track.master_lap.headings),
         json.dumps(sectors_data), json.dumps(sf_data),
         json.dumps(track.master_lap.lats), json.dumps(track.master_lap.lons), json.dumps(track.master_lap.headings)),
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
    headings = json.loads(row["master_lap_headings"]) if row["master_lap_headings"] else [0.0] * len(lats)
    return Track(
        name=track_name,
        sf_line=Line(Point(sf[0], sf[1]), Point(sf[2], sf[3])),
        master_lap=MasterLap(lats=lats, lons=lons, headings=headings),
        sector_split_lines=[Line(Point(c[0], c[1]), Point(c[2], c[3])) for c in sectors_data],
    )


def list_tracks() -> list[str]:
    """Return all saved track names."""
    db = _get_db()
    rows = db.execute("SELECT name FROM tracks ORDER BY name").fetchall()
    db.close()
    return [r["name"] for r in rows]


def serialize_source_session(source_session) -> bytes:
    """Serialize a SourceSession to JSON bytes for storage."""
    data = {
        "metadata": {
            "track": source_session.metadata.track,
            "date": source_session.metadata.date,
            "time": source_session.metadata.time,
            "session_type": source_session.metadata.session_type,
        },
        "channels": {
            name: {
                "timestamps": ch.timestamps,
                "values": ch.values,
            }
            for name, ch in source_session.channels.items()
        },
    }
    return json.dumps(data).encode()


def deserialize_source_session(data: bytes):
    """Deserialize a SourceSession from JSON bytes."""
    from datahawk.source.types import SourceChannel, SourceSession, SourceSessionMetadata
    obj = json.loads(data)
    meta = SourceSessionMetadata(**obj["metadata"])
    channels = {}
    for name, ch_data in obj["channels"].items():
        ch = SourceChannel(name=name)
        ch.timestamps = ch_data["timestamps"]
        ch.values = ch_data["values"]
        channels[name] = ch
    return SourceSession(metadata=meta, channels=channels)
