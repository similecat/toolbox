"""SQLite database module for persistent TTS job storage, user auth, and settings."""
import sqlite3
import os
import re
import secrets
import datetime
import threading

# Database path — lives in data/ at the project root (override via TOOLBOX_DB_PATH)
DB_PATH = os.environ.get(
    "TOOLBOX_DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "toolbox.db"),
)

DEFAULT_ALLOWED_EMAIL = "bo.yang@hotmail.com"
DEFAULT_API_KEY = "tbx-default-api-key-2026"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Thread-local storage for persistent connections (one per thread)
_conn_local = threading.local()


def normalize_email(email: str) -> str:
    """Normalize an email address for case-insensitive comparison and storage."""
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    """Validate basic email address format."""
    norm = normalize_email(email)
    return bool(norm) and len(norm) <= 254 and bool(_EMAIL_RE.match(norm))


def _get_conn():
    """Return a thread-local persistent connection to the SQLite database."""
    current_path = DB_PATH
    if (
        not hasattr(_conn_local, "conn")
        or _conn_local.conn is None
        or getattr(_conn_local, "db_path", None) != current_path
    ):
        if hasattr(_conn_local, "conn") and _conn_local.conn is not None:
            try:
                _conn_local.conn.close()
            except Exception:
                pass
        os.makedirs(os.path.dirname(current_path), exist_ok=True)
        conn = sqlite3.connect(current_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA temp_store=MEMORY")
        _conn_local.conn = conn
        _conn_local.db_path = current_path
    return _conn_local.conn


def init_db():
    """Create tables and seed default settings/allow-list if they don't exist."""
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS allowed_emails (
            email TEXT PRIMARY KEY,
            added_by TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    now = datetime.datetime.now().isoformat()

    # Seed default allowed email once on initial setup
    seeded = conn.execute(
        "SELECT value FROM settings WHERE key = 'allowlist_initialized'"
    ).fetchone()
    if not seeded:
        conn.execute(
            "INSERT OR IGNORE INTO allowed_emails (email, added_by, created_at) VALUES (?, 'system', ?)",
            (normalize_email(DEFAULT_ALLOWED_EMAIL), now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('allowlist_initialized', '1', ?)",
            (now,),
        )

    # Seed default shared API key if not set
    api_key_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'api_key'"
    ).fetchone()
    if not api_key_row:
        initial_api_key = os.environ.get("TOOLBOX_API_KEY") or DEFAULT_API_KEY
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('api_key', ?, ?)",
            (initial_api_key, now),
        )

    # Seed persistent Flask secret key if not set
    secret_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'secret_key'"
    ).fetchone()
    if not secret_row:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('secret_key', ?, ?)",
            (secrets.token_hex(32), now),
        )

    conn.commit()


# ============================================================
# Settings & API Key Management
# ============================================================

def get_setting(key: str, default: str = None):
    """Fetch a setting value by key."""
    conn = _get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    """Insert or update a setting value."""
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )
    conn.commit()


def get_api_key() -> str:
    """Return the active shared REST API key."""
    return get_setting("api_key", DEFAULT_API_KEY)


def set_api_key(new_key: str):
    """Update the shared REST API key."""
    set_setting("api_key", new_key.strip())


# ============================================================
# Email Allow-List Management
# ============================================================

def is_email_allowed(email: str) -> bool:
    """Check whether an email address is in the allow list."""
    norm = normalize_email(email)
    if not norm:
        return False
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM allowed_emails WHERE email = ?", (norm,)
    ).fetchone()
    return row is not None


def list_allowed_emails():
    """Return all allow-listed email entries with registration status."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT a.email, a.added_by, a.created_at,
               CASE WHEN u.id IS NOT NULL THEN 1 ELSE 0 END AS is_registered,
               COALESCE(u.is_admin, 0) AS is_admin
        FROM allowed_emails a
        LEFT JOIN users u ON a.email = u.email
        ORDER BY a.created_at ASC
    """).fetchall()
    return [dict(r) for r in rows]


def add_allowed_email(email: str, added_by: str = "admin") -> bool:
    """Add an email address to the allow list. Returns True if newly added."""
    norm = normalize_email(email)
    if not is_valid_email(norm):
        raise ValueError("Invalid email address format")
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO allowed_emails (email, added_by, created_at) VALUES (?, ?, ?)",
        (norm, added_by, now),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_allowed_email(email: str) -> bool:
    """Remove an email address from the allow list. Returns True if removed."""
    norm = normalize_email(email)
    conn = _get_conn()
    cur = conn.execute("DELETE FROM allowed_emails WHERE email = ?", (norm,))
    conn.commit()
    return cur.rowcount > 0


# ============================================================
# User Account Management
# ============================================================

def count_admins() -> int:
    """Return the number of registered admin users."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_admin = 1").fetchone()
    return row["cnt"] if row else 0


def create_user(email: str, password_hash: str, is_admin: bool = False):
    """Create a new user account. Default email or first admin-less user becomes admin."""
    norm = normalize_email(email)
    if not is_valid_email(norm):
        raise ValueError("Invalid email address format")
    if norm == normalize_email(DEFAULT_ALLOWED_EMAIL) or count_admins() == 0:
        is_admin = True
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO users (email, password_hash, is_admin, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (norm, password_hash, 1 if is_admin else 0, now, now),
    )
    conn.commit()
    return get_user_by_id(cur.lastrowid)


def get_user_by_email(email: str):
    """Return a user dict (including password_hash) by email, or None."""
    norm = normalize_email(email)
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (norm,)).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int):
    """Return a user dict by id, or None."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def update_user_password(email: str, password_hash: str) -> bool:
    """Update password hash for an existing user."""
    norm = normalize_email(email)
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE email = ?",
        (password_hash, now, norm),
    )
    conn.commit()
    return cur.rowcount > 0


def list_users():
    """Return all registered users (excluding password_hash)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, email, is_admin, created_at, updated_at FROM users ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def set_user_admin(user_id: int, is_admin: bool) -> bool:
    """Grant or revoke admin role for a user."""
    now = datetime.datetime.now().isoformat()
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET is_admin = ?, updated_at = ? WHERE id = ?",
        (1 if is_admin else 0, now, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    """Delete a registered user account."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    return cur.rowcount > 0


# ============================================================
# TTS Job Storage
# ============================================================

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
    """Return jobs (optionally filtered by status), newest first."""
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


def delete_job(job_id):
    """Delete a job and its associated audio file (if any)."""
    conn = _get_conn()
    row = conn.execute("SELECT audio_path FROM tts_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.execute("DELETE FROM tts_jobs WHERE id = ?", (job_id,))
    conn.commit()

    if row and row["audio_path"]:
        audio_path = row["audio_path"]
        if os.path.exists(audio_path):
            os.remove(audio_path)


def count_jobs(status_filter=None):
    """Return count of jobs (optionally filtered by status)."""
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
    """Delete completed/failed jobs older than max_age_days."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=max_age_days)).isoformat()
    conn = _get_conn()

    rows = conn.execute(
        "SELECT id, audio_path FROM tts_jobs WHERE status IN ('completed', 'failed') AND updated_at < ?",
        (cutoff,),
    ).fetchall()

    deleted_count = 0
    for row in rows:
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
    """Close the thread-local database connection."""
    if hasattr(_conn_local, "conn") and _conn_local.conn is not None:
        _conn_local.conn.close()
        _conn_local.conn = None
        _conn_local.db_path = None
