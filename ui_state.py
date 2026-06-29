"""
Thread-safe shared state between the monitoring backend and the GUI.
All fields are read by the UI thread and written by the monitoring thread.
Simple int/bool fields are safe to read without a lock under CPython's GIL.
The detections deque uses appendleft() which is atomic in CPython.
"""

import json
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config

_USERS_FILE = os.path.join("data", "users_list.json")


def _load_users() -> list[str]:
    try:
        if os.path.exists(_USERS_FILE):
            with open(_USERS_FILE, encoding="utf-8") as f:
                return [str(u) for u in json.load(f).get("users", []) if u]
    except Exception:
        pass
    return []


def _save_users(users: list[str]) -> None:
    os.makedirs(os.path.dirname(_USERS_FILE), exist_ok=True)
    try:
        with open(_USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": users}, f, indent=2)
    except Exception:
        pass


@dataclass
class Detection:
    """One new-post event shown in the Detections tab."""

    username: str
    display_name: Optional[str]
    post_url: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class UserStatus:
    """Per-user monitoring status shown in the Users tab."""

    username: str
    last_checked: Optional[datetime] = None
    last_post_id: Optional[str] = None
    ok: bool = True


class AppState:
    """
    Central shared state.  Written by monitoring thread, read by UI thread.
    Use update_user() and add_detection() — they are thread-safe.
    """

    def __init__(self) -> None:
        self.is_monitoring: bool = False
        self.started_at: Optional[datetime] = None

        # Counters — mirrored from Scheduler after each cycle
        self.checked_count: int = 0
        self.new_posts_count: int = 0
        self.error_count: int = 0

        # Recent detections (newest first, capped at 50)
        self.detections: deque[Detection] = deque(maxlen=50)

        # Per-user status dict  { "openai": UserStatus, ... }
        self.user_statuses: dict[str, UserStatus] = {}

        # Managed user list (source of truth for add/remove)
        # Load from saved file first; fall back to hardcoded config
        _saved = _load_users()
        self._monitored_users: list[str] = _saved if _saved else list(config.MONITORED_USERS)

        # Display names discovered via search or scraping
        self._display_names: dict[str, str] = {}

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write methods (called from monitoring thread)
    # ------------------------------------------------------------------

    def add_detection(self, detection: Detection) -> None:
        with self._lock:
            self.detections.appendleft(detection)

    def update_user(
        self,
        username: str,
        last_checked: datetime,
        last_post_id: Optional[str] = None,
        ok: bool = True,
    ) -> None:
        with self._lock:
            self.user_statuses[username.lower()] = UserStatus(
                username=username,
                last_checked=last_checked,
                last_post_id=last_post_id,
                ok=ok,
            )

    def sync_from_scheduler(self, scheduler: object) -> None:
        """Copy counter values from a Scheduler instance (no lock needed for ints)."""
        self.checked_count = getattr(scheduler, "checked_count", 0)
        self.new_posts_count = getattr(scheduler, "new_posts_count", 0)
        self.error_count = getattr(scheduler, "error_count", 0)

    # ------------------------------------------------------------------
    # Monitored user list management
    # ------------------------------------------------------------------

    def get_monitored_users(self) -> list[str]:
        with self._lock:
            return list(self._monitored_users)

    def add_user(self, username: str, display_name: Optional[str] = None) -> tuple[bool, str]:
        """Add a user. Returns (success, reason)."""
        username = username.strip().lstrip("@")
        if not username:
            return False, "Empty username"
        with self._lock:
            lower_list = [u.lower() for u in self._monitored_users]
            if username.lower() in lower_list:
                return False, "Already monitoring this user"
            if len(self._monitored_users) >= config.MAX_MONITORED_USERS:
                return False, f"Limit of {config.MAX_MONITORED_USERS} users reached"
            self._monitored_users.append(username)
            config.MONITORED_USERS = list(self._monitored_users)
            if display_name:
                self._display_names[username.lower()] = display_name
            _save_users(self._monitored_users)
            return True, "ok"

    def remove_user(self, username: str) -> bool:
        """Remove a user. Returns True if removed."""
        username_lower = username.lower()
        with self._lock:
            before = len(self._monitored_users)
            self._monitored_users = [u for u in self._monitored_users if u.lower() != username_lower]
            config.MONITORED_USERS = list(self._monitored_users)
            _save_users(self._monitored_users)
            return len(self._monitored_users) < before

    def set_display_name(self, username: str, display_name: str) -> None:
        with self._lock:
            self._display_names[username.lower()] = display_name

    def get_display_name(self, username: str) -> Optional[str]:
        with self._lock:
            return self._display_names.get(username.lower())

    # ------------------------------------------------------------------
    # Read helpers (called from UI thread)
    # ------------------------------------------------------------------

    def get_detections_snapshot(self) -> list[Detection]:
        with self._lock:
            return list(self.detections)

    def get_user_statuses_snapshot(self) -> list[UserStatus]:
        with self._lock:
            return list(self.user_statuses.values())
