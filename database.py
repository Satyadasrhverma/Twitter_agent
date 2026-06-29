"""
SQLite persistence layer using aiosqlite (async).
All public methods are coroutines safe to call from asyncio tasks.
Each row is scoped to an owner_id (app user id).
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
    """Async SQLite wrapper. All queries are scoped to self._owner_id."""

    def __init__(self, db_path: str = config.DB_PATH, owner_id: int = 0) -> None:
        self._db_path  = db_path
        self._owner_id = owner_id
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()
        logger.debug("Database opened: %s (owner=%d)", self._db_path, self._owner_id)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.debug("Database closed")

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_tables(self) -> None:
        assert self._conn is not None
        # app_users table (managed by app_auth.py via sync sqlite)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS app_users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                created_at    TEXT    NOT NULL
            )
        """)
        # monitored_users table scoped per app-user
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS monitored_users (
                owner_id               INTEGER NOT NULL DEFAULT 0,
                username               TEXT    NOT NULL,
                latest_post_id         TEXT,
                latest_post_url        TEXT,
                last_checked           TEXT,
                last_notification_time TEXT,
                PRIMARY KEY (owner_id, username)
            )
        """)
        await self._conn.commit()
        logger.debug("Schema verified")

    # ------------------------------------------------------------------
    # CRUD (all scoped to self._owner_id)
    # ------------------------------------------------------------------

    async def get_user(self, username: str) -> Optional[UserRecord]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM monitored_users WHERE owner_id=? AND username=?",
            (self._owner_id, username.lower()),
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
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO monitored_users
                (owner_id, username, latest_post_id, latest_post_url,
                 last_checked, last_notification_time)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, username) DO UPDATE SET
                latest_post_id         = excluded.latest_post_id,
                latest_post_url        = excluded.latest_post_url,
                last_checked           = excluded.last_checked,
                last_notification_time = excluded.last_notification_time
            """,
            (
                self._owner_id,
                record.username.lower(),
                record.latest_post_id,
                record.latest_post_url,
                _to_iso(record.last_checked),
                _to_iso(record.last_notification_time),
            ),
        )
        await self._conn.commit()
        logger.debug("Upserted record for @%s (owner=%d)", record.username, self._owner_id)

    async def update_last_checked(self, username: str, ts: datetime) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO monitored_users (owner_id, username, last_checked)
            VALUES (?, ?, ?)
            ON CONFLICT(owner_id, username) DO UPDATE SET
                last_checked = excluded.last_checked
            """,
            (self._owner_id, username.lower(), _to_iso(ts)),
        )
        await self._conn.commit()

    async def get_all_usernames(self) -> list[str]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT username FROM monitored_users WHERE owner_id=? ORDER BY username",
            (self._owner_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["username"] for row in rows]

    async def seed_user(self, username: str, post_id: str, post_url: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT INTO monitored_users (owner_id, username, latest_post_id, latest_post_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(owner_id, username) DO NOTHING
            """,
            (self._owner_id, username.lower(), post_id, post_url),
        )
        await self._conn.commit()
        logger.debug("Seeded initial tweet for @%s (owner=%d)", username, self._owner_id)

    async def remove_user(self, username: str) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM monitored_users WHERE owner_id=? AND username=?",
            (self._owner_id, username.lower()),
        )
        await self._conn.commit()
        logger.debug("Removed DB record for @%s (owner=%d)", username, self._owner_id)
