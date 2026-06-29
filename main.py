"""
Entry point.

  python main.py           → monitoring + web dashboard at http://localhost:8080
  python main.py --no-gui  → headless terminal-only mode
"""

import asyncio
import logging
import signal
import sys

_logger = logging.getLogger(__name__)


# ── Web + monitoring mode (default) ─────────────────────────────────────────

def run() -> None:
    import logger as log_setup
    log_setup.setup_logging()

    import app_auth
    from notifier import ToastNotifier
    from gui import UserMonitorManager
    from web import start_server, PORT

    app_auth.init_tables()

    notifier     = ToastNotifier()
    user_manager = UserMonitorManager(notifier)

    # Start monitoring for every already-registered app user
    for u in app_auth.get_all_users():
        try:
            user_manager.ensure_running(u["id"])
            _logger.info("Started monitor for app user '%s' (id=%d)", u.get("email", u.get("username", "?")), u["id"])
        except Exception as exc:
            _logger.warning("Could not start monitor for user %d: %s", u["id"], exc)

    # Start web server + open browser
    start_server(user_manager, open_browser=True)
    _logger.info("Dashboard -> http://localhost:%d", PORT)

    # Block main thread until Ctrl-C
    try:
        signal.pause()          # Unix
    except (AttributeError, OSError):
        import time             # Windows fallback
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    user_manager.stop_all()


# ── Headless terminal mode ───────────────────────────────────────────────────

def run_headless() -> None:
    import config
    import logger as log_setup
    from browser import BrowserPool
    from database import Database
    from notifier import ToastNotifier
    from scheduler import Scheduler
    from utils import status_display_loop

    async def _main() -> None:
        log_setup.setup_logging()
        _logger.info("X Monitor starting (headless) — %d users", len(config.MONITORED_USERS))

        db = Database()
        await db.connect()
        notifier = ToastNotifier()

        async with BrowserPool(pool_size=config.WORKER_COUNT) as pool:
            scheduler = Scheduler(
                users=config.MONITORED_USERS,
                browser_pool=pool,
                database=db,
                notifier=notifier,
            )

            try:
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, lambda: asyncio.create_task(scheduler.shutdown()))
            except (NotImplementedError, AttributeError):
                signal.signal(signal.SIGTERM,
                               lambda *_: asyncio.get_event_loop().create_task(scheduler.shutdown()))

            status_task = asyncio.create_task(status_display_loop(scheduler), name="status")
            try:
                await scheduler.run()
            finally:
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError:
                    pass

        await db.close()
        _logger.info("Shut down cleanly")

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger(__name__).exception("Fatal: %s", exc)
        sys.exit(1)


# ── Entry ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--no-gui" in sys.argv:
        run_headless()
    else:
        run()
