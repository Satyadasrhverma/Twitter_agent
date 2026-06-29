"""
App-level auth — Google OAuth only.
Users sign in with Google, auto-registered on first login, 30-day session cookie.
"""

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

import config

_lock      = threading.Lock()
_sessions: dict[str, dict] = {}   # token → {user_id, username, email}


# ── DB ────────────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _add_col(conn, table: str, col: str, definition: str) -> None:
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")


def init_tables() -> None:
    with _conn() as c:
        # Detect old password-based schema (has 'username' column) and drop it
        existing_cols = [r[1] for r in c.execute("PRAGMA table_info(app_users)").fetchall()]
        if existing_cols and 'username' in existing_cols:
            c.execute("DROP TABLE app_users")

        c.execute("""
            CREATE TABLE IF NOT EXISTS app_users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                google_id  TEXT    UNIQUE NOT NULL,
                email      TEXT    NOT NULL DEFAULT '',
                name       TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            )
        """)
        # Migration: add columns to older schema if needed
        _add_col(c, 'app_users', 'google_id', 'TEXT')
        _add_col(c, 'app_users', 'email',     'TEXT NOT NULL DEFAULT ""')
        _add_col(c, 'app_users', 'name',      'TEXT NOT NULL DEFAULT ""')
        c.commit()


# ── Session ───────────────────────────────────────────────────────────────────

def _make_token(user_id: int, email: str, name: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {'user_id': user_id, 'username': email, 'email': email, 'name': name}
    return token


def get_session(token: str) -> Optional[dict]:
    with _lock:
        return _sessions.get(token)


def logout(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)


# ── Google OAuth ──────────────────────────────────────────────────────────────

def get_or_create_google_user(google_id: str, email: str, name: str) -> tuple[str, int]:
    """Returns (session_token, user_id). Auto-creates on first login."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, email, name FROM app_users WHERE google_id=?", (google_id,)
        ).fetchone()
        if row:
            # Update name/email in case they changed
            c.execute(
                "UPDATE app_users SET email=?, name=? WHERE google_id=?",
                (email, name, google_id)
            )
            c.commit()
            return _make_token(int(row['id']), email, name), int(row['id'])

        # First login — auto-register
        c.execute(
            "INSERT INTO app_users (google_id, email, name, created_at) VALUES (?,?,?,?)",
            (google_id, email, name, datetime.now(timezone.utc).isoformat()),
        )
        c.commit()
        user_id = int(c.execute(
            "SELECT id FROM app_users WHERE google_id=?", (google_id,)
        ).fetchone()['id'])

    return _make_token(user_id, email, name), user_id


# ── Admin helpers ─────────────────────────────────────────────────────────────

def get_all_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, google_id, email, name, created_at FROM app_users ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
