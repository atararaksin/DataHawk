"""Persistent storage: SQLite metadata + session files."""

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
