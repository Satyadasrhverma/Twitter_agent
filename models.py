"""
Shared data models used across all modules.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class UserRecord:
    """Represents a monitored X user's persisted state."""

    username: str
    latest_post_id: Optional[str] = None
    latest_post_url: Optional[str] = None
    last_checked: Optional[datetime] = None
    last_notification_time: Optional[datetime] = None


@dataclass
class PostInfo:
    """Represents a scraped post extracted from a profile page."""

    post_id: str
    post_url: str
    username: str
    display_name: Optional[str] = None
    detected_at: datetime = field(default_factory=datetime.now)


@dataclass
class MonitorResult:
    """Result returned by monitor.check_user()."""

    username: str
    success: bool
    post: Optional[PostInfo] = None
    is_new: bool = False
    error: Optional[str] = None
