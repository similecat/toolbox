"""SQLite database module for persistent TTS job storage."""
import sqlite3
import os
import datetime
import threading

# Database path — lives in data/ at the project root
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "toolbox.db")

# Thread-local storage for persistent connections (one per thread)
_conn_local = threading.local()


def _get_conn():
    """Return a thread-local persistent connection to the SQLite database.
    
    Each thread gets its own connection, avoiding lock contention from
    thousands of open/close cycles. WAL mode + synchronous=NORMAL enables
    high-concurrency reads without blocking writes.
    """
    if not hasattr(_conn_local, 'conn') or _conn_local.conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")        # concurrent read/write
        conn.execute("PRAGMA synchronous=NORMAL")       # faster writes, safe for job queue
        conn.execute("PRAGMA cache_size=-64000")        # 64MB page cache
        conn.execute("PRAGMA temp_store=MEMORY")        # faster temp operations
        _conn_local.conn = conn
    return _conn_local.conn


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


def add_job(job_id, text, language="en"):
    """Insert a new job row."""
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tts_jobs (id, text, language, status, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
        (job_id, text, language, now, now),
    )
    conn.commit()


def get_job(job_id):
    """Return a single job dict (or None)."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tts_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(status_filter=None, limit=100, offset=0):
    """Return jobs (optionally filtered by status), newest first.
    
    Args:
        status_filter: Filter by status (e.g. 'pending', 'completed').
        limit: Maximum number of rows to return (default 100).
        offset: Number of rows to skip for pagination (default 0).
    """
    conn = _get_conn()
    base = "SELECT * FROM tts_jobs"
    where = " WHERE status = ?" if status_filter else ""
    
    if status_filter:
        rows = conn.execute(
            f"{base}{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status_filter, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            f"{base} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
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

    # Remove audio file from disk
    if row and row["audio_path"]:
        audio_path = row["audio_path"]
        if os.path.exists(audio_path):
            os.remove(audio_path)


def count_jobs(status_filter=None):
    """Return count of jobs (optionally filtered by status).
    
    Lightweight alternative to list_jobs() — returns a single integer
    instead of full row data. Ideal for worker polling.
    """
    conn = _get_conn()
    if status_filter:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tts_jobs WHERE status = ?",
            (status_filter,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tts_jobs"
        ).fetchone()
    return row["cnt"] if row else 0


def cleanup_old_jobs(max_age_days=7):
    """Delete completed/failed jobs older than max_age_days.
    
    Prevents unbounded table growth. Also removes associated audio files.
    
    Returns:
        int: Number of jobs deleted.
    """
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=max_age_days)).isoformat()
    conn = _get_conn()
    
    # Get rows to delete
    rows = conn.execute(
        "SELECT id, audio_path FROM tts_jobs WHERE status IN ('completed', 'failed') AND updated_at < ?",
        (cutoff,),
    ).fetchall()
    
    deleted_count = 0
    for row in rows:
        # Remove audio file from disk
        if row["audio_path"] and os.path.exists(row["audio_path"]):
            try:
                os.remove(row["audio_path"])
            except OSError:
                pass
        conn.execute("DELETE FROM tts_jobs WHERE id = ?", (row["id"],))
        deleted_count += 1
    
    conn.commit()
    return deleted_count


def close_conn():
    """Close the thread-local database connection.
    
    Call this on application shutdown to release the file handle.
    """
    if hasattr(_conn_local, 'conn') and _conn_local.conn is not None:
        _conn_local.conn.close()
        _conn_local.conn = None
