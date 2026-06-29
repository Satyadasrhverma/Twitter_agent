"""
SQLite persistence layer using aiosqlite (async).
All public methods are coroutines safe to call from asyncio tasks.
"""

import logging
import os
from datetime import datetime
from typing import Optional

import aiosqlite

import config
from models import UserRecord

logger = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(_ISO_FMT) if dt else None


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    return datetime.strptime(s, _ISO_FMT) if s else None


class Database:
    """Async SQLite wrapper for monitored user state."""

    def __init__(self, db_path: str = config.DB_PATH) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database connection and ensure tables exist."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        # WAL mode: better concurrent read performance
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()
        logger.debug("Database opened: %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection cleanly."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.debug("Database closed")

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_tables(self) -> None:
        """Create tables if they do not already exist."""
        assert self._conn is not None
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitored_users (
                username                TEXT PRIMARY KEY NOT NULL,
                latest_post_id          TEXT,
                latest_post_url         TEXT,
                last_checked            TEXT,
                last_notification_time  TEXT
            )
            """
        )
        await self._conn.commit()
        logger.debug("Schema verified")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def get_user(self, username: str) -> Optional[UserRecord]:
        """Return the stored record for *username*, or None if unseen."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM monitored_users WHERE username = ?",
            (username.lower(),),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        return UserRecord(
            username=row["username"],
            latest_post_id=row["latest_post_id"],
            latest_post_url=row["latest_post_url"],
            last_checked=_from_iso(row["last_checked"]),
            last_notification_time=_from_iso(row["last_notification_time"]),
        )

    async def upsert_user(self, record: UserRecord) -> None:
        """Insert or update the full record for *username*."""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO monitored_users
                (username, latest_post_id, latest_post_url,
                 last_checked, last_notification_time)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                latest_post_id         = excluded.latest_post_id,
                latest_post_url        = excluded.latest_post_url,
                last_checked           = excluded.last_checked,
                last_notification_time = excluded.last_notification_time
            """,
            (
                record.username.lower(),
                record.latest_post_id,
                record.latest_post_url,
                _to_iso(record.last_checked),
                _to_iso(record.last_notification_time),
            ),
        )
        await self._conn.commit()
        logger.debug("Upserted record for @%s", record.username)

    async def update_last_checked(self, username: str, ts: datetime) -> None:
        """Update only the last_checked timestamp without touching post data."""
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO monitored_users (username, last_checked)
            VALUES (?, ?)
            ON CONFLICT(username) DO UPDATE SET
                last_checked = excluded.last_checked
            """,
            (username.lower(), _to_iso(ts)),
        )
        await self._conn.commit()

    async def get_all_usernames(self) -> list[str]:
        """Return every username currently stored in the database."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT username FROM monitored_users ORDER BY username"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["username"] for row in rows]

    async def seed_user(self, username: str, post_id: str, post_url: str) -> None:
        """
        Insert an initial tweet ID for a newly-added user.
        No-op if a record already exists — never overwrites existing data.
        This prevents a false 'new post' alert on the very first check.
        """
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO monitored_users (username, latest_post_id, latest_post_url)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO NOTHING
            """,
            (username.lower(), post_id, post_url),
        )
        await self._conn.commit()
        logger.debug("Seeded initial tweet ID for @%s: %s", username, post_id)

    async def remove_user(self, username: str) -> None:
        """Remove a user record. Kept separate so history can optionally be preserved."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM monitored_users WHERE username = ?",
            (username.lower(),),
        )
        await self._conn.commit()
        logger.debug("Removed DB record for @%s", username)
