"""SQLite database module for persistent TTS job storage."""
import sqlite3
import os
import datetime

# Database path — lives in data/ at the project root
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "toolbox.db")


def _get_conn():
    """Return a connection to the SQLite database."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read/write
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tts_jobs (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en',
            status TEXT NOT NULL DEFAULT 'pending',
            audio_path TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_status ON tts_jobs(status)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_created_at ON tts_jobs(created_at)
    """)
    conn.commit()
    conn.close()


def add_job(job_id, text, language="en"):
    """Insert a new job row."""
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tts_jobs (id, text, language, status, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
        (job_id, text, language, now, now),
    )
    conn.commit()
    conn.close()


def get_job(job_id):
    """Return a single job dict (or None)."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tts_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_jobs(status_filter=None):
    """Return all jobs (optionally filtered by status), newest first."""
    conn = _get_conn()
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM tts_jobs WHERE status = ? ORDER BY created_at DESC",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tts_jobs ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_job_status(job_id, status, error=None):
    """Update status (and optional error) for a job."""
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    if error is not None:
        conn.execute(
            "UPDATE tts_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, now, job_id),
        )
    else:
        conn.execute(
            "UPDATE tts_jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, job_id),
        )
    conn.commit()
    conn.close()


def update_job_audio_path(job_id, audio_path):
    """Mark a job as completed and store the audio file path."""
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "UPDATE tts_jobs SET status = 'completed', audio_path = ?, updated_at = ? WHERE id = ?",
        (audio_path, now, job_id),
    )
    conn.commit()
    conn.close()


def delete_job(job_id):
    """Delete a job and its associated audio file (if any)."""
    conn = _get_conn()
    row = conn.execute("SELECT audio_path FROM tts_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.execute("DELETE FROM tts_jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()

    # Remove audio file from disk
    if row and row["audio_path"]:
        audio_path = row["audio_path"]
        if os.path.exists(audio_path):
            os.remove(audio_path)
