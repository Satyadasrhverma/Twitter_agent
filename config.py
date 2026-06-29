"""
Central configuration. All tunable constants live here.
Set overrides via environment variables (e.g. MONITOR_HEADLESS=false).
"""

import os
from typing import Final

from dotenv import load_dotenv
load_dotenv()  # auto-loads .env file from project folder

# All paths are resolved relative to this file's directory so the app works
# no matter which directory it's launched from.
_BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Google OAuth (set via environment variables or .env)
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID: Final[str] = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: Final[str] = os.getenv("GOOGLE_CLIENT_SECRET", "")

# ---------------------------------------------------------------------------
# Monitoring targets
# ---------------------------------------------------------------------------

MONITORED_USERS: list[str] = []

# Maximum users allowed
MAX_MONITORED_USERS: Final[int] = 100

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# Seconds between full cycles (all users checked once = one cycle)
CHECK_INTERVAL_SECONDS: Final[int] = int(os.getenv("MONITOR_INTERVAL", "30"))

# Max seconds to wait for a profile page to load
PAGE_TIMEOUT_MS: Final[int] = int(os.getenv("MONITOR_PAGE_TIMEOUT_MS", "20000"))

# Seconds to wait between retries on failure
RETRY_WAIT_SECONDS: Final[int] = int(os.getenv("MONITOR_RETRY_WAIT", "5"))

# Max retry attempts per user per cycle
MAX_RETRIES: Final[int] = int(os.getenv("MONITOR_MAX_RETRIES", "2"))

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

# Browser page pool size (how many Playwright pages are kept open)
WORKER_COUNT: Final[int] = int(os.getenv("MONITOR_WORKERS", "5"))

# How many users are checked simultaneously in one cycle
MAX_CONCURRENT: Final[int] = int(os.getenv("MONITOR_CONCURRENT", "10"))

# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

# Run browser without visible window
HEADLESS: Final[bool] = os.getenv("MONITOR_HEADLESS", "true").lower() != "false"

# Chromium user-agent string (avoid bot fingerprinting)
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# Viewport dimensions
VIEWPORT_WIDTH: Final[int] = 1280
VIEWPORT_HEIGHT: Final[int] = 900

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

DB_PATH: Final[str] = os.getenv("MONITOR_DB_PATH", os.path.join(_BASE_DIR, "data", "monitor.db"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR: Final[str] = os.path.join(_BASE_DIR, "logs")
LOG_FILE: Final[str] = os.path.join(_BASE_DIR, "logs", "monitor.log")

# Bytes before log rotation (10 MB)
LOG_MAX_BYTES: Final[int] = 10 * 1024 * 1024

# Number of rotated log files to keep
LOG_BACKUP_COUNT: Final[int] = 5

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

# App name shown in Windows notification centre
NOTIFICATION_APP_NAME: Final[str] = "X Monitor"

# Duration the toast stays visible (in seconds, winotify uses string)
NOTIFICATION_DURATION: Final[str] = "short"  # "short" = 7s, "long" = 25s

# Play system sound with notification
NOTIFICATION_SOUND: Final[bool] = os.getenv("MONITOR_SOUND", "true").lower() != "false"

# ---------------------------------------------------------------------------
# X / Twitter URLs
# ---------------------------------------------------------------------------

X_BASE_URL: Final[str] = "https://x.com"
