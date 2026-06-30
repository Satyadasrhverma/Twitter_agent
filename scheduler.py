"""
Async worker scheduler.
Divides the monitored user list into N shards and runs each shard as an
independent asyncio Task.  Each task loops: check every assigned user →
sleep until next cycle → repeat.
"""

import asyncio
import logging
import socket
from datetime import datetime, timezone
from typing import List, Optional

import app_auth
import config
import whatsapp
from browser import BrowserPool
from database import Database
from models import MonitorResult, UserRecord
from monitor import ProfileMonitor
from notifier import ToastNotifier

# Optional import — ui_state is only present when running with GUI
try:
    from ui_state import AppState as _AppState
except ImportError:
    _AppState = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

_NET_ERRORS = ("ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED", "ERR_NAME_NOT_RESOLVED")


def _is_connected() -> bool:
    try:
        socket.setdefaulttimeout(3)
        socket.create_connection(("8.8.8.8", 53)).close()
        return True
    except OSError:
        return False


class Scheduler:
    """
    Divides the user list into WORKER_COUNT shards.
    Each shard runs as an independent asyncio Task with its own error
    boundary — one crashing worker does not affect the others.
    """

    def __init__(
        self,
        users: List[str],
        browser_pool: BrowserPool,
        database: Database,
        notifier: ToastNotifier,
        app_state: Optional[object] = None,
    ) -> None:
        self._users = users
        self._pool = browser_pool
        self._db = database
        self._notifier = notifier
        self._state = app_state          # AppState | None
        self._monitor = ProfileMonitor(browser_pool)
        self._tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

        # Live stats — read by utils.build_status_table() and AppState
        self.checked_count: int = 0
        self.new_posts_count: int = 0
        self.error_count: int = 0
        self.started_at: datetime = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Launch a single parallel worker that checks all users concurrently
        using asyncio.gather + Semaphore(MAX_CONCURRENT).
        """
        initial = self._users if not self._state else self._state.get_monitored_users()
        if not initial:
            logger.warning("No users configured — waiting for users to be added via web dashboard…")
            while not self._shutdown_event.is_set():
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=5.0)
                    return
                except asyncio.TimeoutError:
                    pass
                current = self._state.get_monitored_users() if self._state else self._users
                if current:
                    initial = current
                    logger.info("Users added — starting monitoring with %d user(s)", len(current))
                    break
            else:
                return

        logger.info(
            "Starting parallel monitoring — max %d concurrent checks, interval: %ds",
            config.MAX_CONCURRENT, config.CHECK_INTERVAL_SECONDS,
        )

        task = asyncio.create_task(self._supervised_worker(0), name="worker-0")
        self._tasks.append(task)

        await self._shutdown_event.wait()

        for t in self._tasks:
            t.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("All workers stopped")

    async def shutdown(self) -> None:
        """Signal all workers to stop after their current check completes."""
        logger.info("Shutdown requested — stopping workers after current cycle")
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Worker supervision
    # ------------------------------------------------------------------

    async def _supervised_worker(self, worker_id: int) -> None:
        """
        Wraps _worker with automatic restart on unexpected failure.
        A worker that crashes is restarted after a short delay — up to
        indefinitely, until shutdown is requested.
        """
        restart_delay = 5.0

        while not self._shutdown_event.is_set():
            try:
                await self._worker(worker_id)
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "Worker %d crashed: %s — restarting in %.0fs",
                    worker_id, exc, restart_delay,
                )
                self.error_count += 1
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=restart_delay,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------
    # Core worker loop
    # ------------------------------------------------------------------

    async def _worker(self, worker_id: int) -> None:
        """
        Each cycle: fire all users in parallel, capped at MAX_CONCURRENT.
        Re-reads the live user list every cycle so add/remove takes effect immediately.
        """
        logger.debug("Parallel worker entering main loop")
        sem = asyncio.Semaphore(config.MAX_CONCURRENT)

        async def _bounded(username: str) -> None:
            async with sem:
                if self._shutdown_event.is_set():
                    return
                try:
                    await asyncio.wait_for(self._check_one(username, 0), timeout=120.0)
                except asyncio.TimeoutError:
                    logger.warning("Check timed out for @%s — skipping this cycle", username)
                    self.error_count += 1

        _offline_logged = False

        while not self._shutdown_event.is_set():
            # Pause silently when internet is down — retry every 10s
            if not await asyncio.to_thread(_is_connected):
                if not _offline_logged:
                    logger.warning("Internet disconnected — monitoring paused, will resume automatically")
                    _offline_logged = True
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=10.0)
                    return
                except asyncio.TimeoutError:
                    pass
                continue

            if _offline_logged:
                logger.info("Internet restored — resuming monitoring")
                _offline_logged = False

            cycle_start = asyncio.get_event_loop().time()

            current_users = self._state.get_monitored_users() if self._state else self._users

            if current_users:
                await asyncio.gather(
                    *[_bounded(u) for u in current_users],
                    return_exceptions=True,
                )

            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_for = max(0.0, config.CHECK_INTERVAL_SECONDS - elapsed)

            logger.debug(
                "Cycle done in %.1fs (%d users) — sleeping %.1fs",
                elapsed, len(current_users), sleep_for,
            )

            if sleep_for > 0:
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=sleep_for,
                    )
                    return  # shutdown arrived during sleep
                except asyncio.TimeoutError:
                    pass  # normal path — start next cycle

    # ------------------------------------------------------------------
    # Single-user check
    # ------------------------------------------------------------------

    async def _check_one(self, username: str, worker_id: int) -> None:
        """Run one check for *username* and handle the result."""
        logger.debug("Worker %d checking @%s", worker_id, username)

        result: MonitorResult = await self._monitor.check_user(username)
        now = datetime.now(timezone.utc)

        if not result.success:
            self.error_count += 1
            logger.warning("Check failed for @%s: %s", username, result.error)
            await self._db.update_last_checked(username, now)
            if self._state:
                self._state.update_user(username, now, ok=False)  # type: ignore[union-attr]
                self._state.sync_from_scheduler(self)             # type: ignore[union-attr]
            return

        self.checked_count += 1

        if result.post is None:
            await self._db.update_last_checked(username, now)
            if self._state:
                self._state.update_user(username, now, ok=True)   # type: ignore[union-attr]
                self._state.sync_from_scheduler(self)             # type: ignore[union-attr]
            return

        await self._process_result(result, now)

    async def _process_result(self, result: MonitorResult, now: datetime) -> None:
        """
        Compare the scraped post against the database record.
        Notify and persist only if the post ID has changed.
        """
        assert result.post is not None
        username = result.username.lower()
        new_post_id = result.post.post_id

        stored: Optional[UserRecord] = await self._db.get_user(username)

        # "first seen" means never successfully scraped before
        # (stored is None OR stored has no post_id — e.g. previous check failed)
        is_first_seen = (stored is None) or (stored.latest_post_id is None)
        is_new = is_first_seen or (stored.latest_post_id != new_post_id)  # type: ignore[union-attr]

        # Cache display name whenever we see it
        if result.post.display_name and self._state:
            self._state.set_display_name(username, result.post.display_name)  # type: ignore[union-attr]

        if is_new:
            if not is_first_seen:
                logger.info("NEW POST @%s — id=%s  url=%s", username, new_post_id, result.post.post_url)
                self._notifier.send(result.post)
                self.new_posts_count += 1
                if self._state:
                    from ui_state import Detection
                    self._state.add_detection(Detection(       # type: ignore[union-attr]
                        username=result.post.username,
                        display_name=result.post.display_name,
                        post_url=result.post.post_url,
                    ))
                await self._send_whatsapp(result)
            else:
                logger.info("First seen @%s — seeding post_id=%s (no notification)", username, new_post_id)

            record = UserRecord(
                username=username,
                latest_post_id=new_post_id,
                latest_post_url=result.post.post_url,
                last_checked=now,
                last_notification_time=now if not is_first_seen else None,
            )
            await self._db.upsert_user(record)

        else:
            await self._db.update_last_checked(username, now)
            logger.debug("@%s — no change (post_id=%s)", username, new_post_id)

        # Always sync user status to UI
        if self._state:
            self._state.update_user(username, now, last_post_id=new_post_id, ok=True)  # type: ignore[union-attr]
            self._state.sync_from_scheduler(self)                                        # type: ignore[union-attr]

    async def _send_whatsapp(self, result: MonitorResult) -> None:
        """Best-effort WhatsApp alert to this app-user's saved number. Never raises."""
        if not self._state or not whatsapp.is_configured():
            return
        user_id = getattr(self._state, "user_id", 0)
        if not user_id:
            return
        try:
            number = await asyncio.to_thread(app_auth.get_whatsapp_number, user_id)
            if not number:
                return
            assert result.post is not None
            name = result.post.display_name or f"@{result.post.username}"
            msg = f"X Monitor Alert\n{name} posted something new:\n{result.post.post_url}"
            await asyncio.to_thread(whatsapp.send_whatsapp, number, msg)
        except Exception as exc:
            logger.warning("WhatsApp notify failed for user %d: %s", user_id, exc)
