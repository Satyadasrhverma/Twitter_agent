"""
GUI entry point: system tray icon + customtkinter dashboard window.

Architecture
------------
- Main thread  : tkinter / customtkinter event loop (Dashboard)
- Thread-1     : pystray system tray icon
- Thread-2     : asyncio monitoring loop (BrowserPool + Scheduler)

Communication  : AppState (shared memory, CPython GIL + explicit locks)
                 queue.Queue for tray → tkinter commands
"""

import asyncio
import logging
import queue
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageDraw
import pystray

import auth
import config
import logger as log_setup
from browser import BrowserPool
from database import Database
from notifier import ToastNotifier
from scheduler import Scheduler
from ui_state import AppState

# ── Appearance ──────────────────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

_X_BLUE   = "#1d9bf0"
_GREEN    = "#00ba7c"
_RED      = "#f4212e"
_GRAY     = "#71767b"
_BG_CARD  = "#16181c"
_BG_MAIN  = "#000000"

# ── Tray icon image ──────────────────────────────────────────────────────────

def _make_tray_icon(active: bool = True) -> Image.Image:
    """Draw a 64×64 circle with an X mark inside."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    fill = "#1d9bf0" if active else "#71767b"
    d.ellipse([2, 2, 62, 62], fill=fill)
    # Bold X
    d.line([(18, 18), (46, 46)], fill="white", width=7)
    d.line([(46, 18), (18, 46)], fill="white", width=7)
    return img


# ── Monitoring backend thread ────────────────────────────────────────────────

_SEARCH_TITLE_RE = re.compile(r"^(.+?)\s*\(@[^)]+\)")


class MonitorThread(threading.Thread):
    """Runs the full async monitoring stack in a background thread."""

    def __init__(self, state: AppState, notifier: ToastNotifier) -> None:
        super().__init__(daemon=True, name="monitor-thread")
        self._state    = state
        self._notifier = notifier
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._scheduler: Optional[Scheduler] = None
        self._pool: Optional[BrowserPool] = None

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_run())
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("Monitor thread error: %s", exc)
        finally:
            self._loop.close()

    async def _async_run(self) -> None:
        _logger = logging.getLogger(__name__)

        self._state.is_monitoring = True
        self._state.started_at    = datetime.now(timezone.utc)

        db = Database()
        await db.connect()

        # Auto-import Twitter session from Edge/Chrome on every startup.
        # This means the user never has to click "Import" manually — as long
        # as they stay logged in to x.com in their browser, it just works.
        try:
            ok, src = await asyncio.to_thread(auth.import_from_browser)
            if ok:
                _logger.info("Auto-imported Twitter session from %s on startup", src)
            else:
                _logger.info("Auto-import skipped (%s) — using stored session if available", src)
        except Exception as exc:
            _logger.debug("Auto-import on startup failed: %s", exc)

        while True:
            session = auth.SESSION_PATH if auth.session_exists() else None
            async with BrowserPool(pool_size=config.WORKER_COUNT, session_path=session) as pool:
                self._pool = pool
                self._scheduler = Scheduler(
                    users        = config.MONITORED_USERS,
                    browser_pool = pool,
                    database     = db,
                    notifier     = self._notifier,
                    app_state    = self._state,
                )
                sched_task = asyncio.create_task(self._scheduler.run(), name="scheduler")
                watch_task = asyncio.create_task(
                    self._watch_for_session_reload(self._scheduler), name="session-watcher"
                )
                done, pending = await asyncio.wait(
                    [sched_task, watch_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            self._pool = None

            if auth.consume_session_reload():
                _logger.info("New Twitter session detected — reloading browser pool…")
                continue  # restart the loop with the new session
            break

        await db.close()
        self._state.is_monitoring = False

    async def _watch_for_session_reload(self, scheduler: Scheduler) -> None:
        """Trigger a browser pool reload when a new Twitter session is saved."""
        while True:
            await asyncio.sleep(5)
            if auth.check_session_reload():
                await scheduler.shutdown()
                return

    def stop(self) -> None:
        """Thread-safe shutdown — callable from the UI thread."""
        if self._scheduler and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._scheduler.shutdown(), self._loop
            )

    def search_user(self, username: str) -> Optional[dict]:
        """
        Blocking call: submit a quick profile lookup to the monitor event loop.
        Returns dict with keys: found, username, display_name, reason.
        """
        if not self._loop or not self._loop.is_running():
            return {"found": False, "reason": "Monitor not ready yet — wait a few seconds"}
        future = asyncio.run_coroutine_threadsafe(
            self._async_search(username), self._loop
        )
        try:
            return future.result(timeout=25)
        except Exception as exc:
            return {"found": False, "reason": str(exc)}

    def search_users_by_name(self, query: str) -> list[dict]:
        """
        Multi-result Twitter search — works best with a saved login session.
        Submits to the monitor event loop; blocks up to 30 s.
        """
        if not self._loop or not self._loop.is_running():
            return []
        future = asyncio.run_coroutine_threadsafe(
            self._async_search_by_name(query), self._loop
        )
        try:
            return future.result(timeout=30)
        except Exception:
            return []

    async def _async_search_by_name(self, query: str) -> list[dict]:
        import urllib.parse
        if self._pool is None:
            return []
        url = (
            f"https://x.com/search?q={urllib.parse.quote(query)}"
            f"&f=user&src=typed_query"
        )
        try:
            async with self._pool.lease() as page:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
                await asyncio.sleep(3)

                results: list[dict] = []

                # React-rendered user cells (available when logged in)
                cells = await page.query_selector_all('[data-testid="UserCell"]')
                for cell in cells[:6]:
                    try:
                        username: Optional[str] = None
                        for a in await cell.query_selector_all('a[href^="/"]'):
                            href = await a.get_attribute("href") or ""
                            parts = [p for p in href.split("/") if p]
                            if len(parts) == 1 and parts[0] not in ("home", "explore", "notifications"):
                                username = parts[0]
                                break
                        if not username:
                            continue
                        display_name = username
                        for span in await cell.query_selector_all('[data-testid="UserName"] span'):
                            t = (await span.inner_text()).strip()
                            if t and not t.startswith("@"):
                                display_name = t
                                break
                        results.append({"username": username,
                                        "display_name": display_name,
                                        "found": True})
                    except Exception:
                        continue

                return results
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).error("search_by_name failed: %s", exc)
            return []

    def get_latest_post(self, username: str) -> Optional[dict]:
        """
        Blocking call: fetch the latest tweet for *username* via the fast API
        path (uses browser context cookies — no page lease needed).
        Returns {"post_id": ..., "post_url": ...} or None.
        """
        if not self._loop or not self._loop.is_running() or self._pool is None:
            return None
        from monitor import ProfileMonitor
        pm = ProfileMonitor(self._pool)
        future = asyncio.run_coroutine_threadsafe(
            pm._fetch_via_api(username), self._loop
        )
        try:
            post = future.result(timeout=5)
            if post:
                return {"post_id": post.post_id, "post_url": post.post_url}
        except Exception:
            pass
        return None

    async def _async_search(self, username: str) -> dict:
        if self._pool is None:
            return {"found": False, "reason": "Browser pool not ready"}
        try:
            async with self._pool.lease() as page:
                try:
                    await page.goto(
                        f"https://x.com/{username}",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(1.5)
                title = await page.title()
                m = _SEARCH_TITLE_RE.match(title)
                if m:
                    name = m.group(1).strip()
                    return {"username": username, "display_name": name, "found": True}
                try:
                    body = (await page.inner_text("body")).lower()
                    if any(p in body for p in ["account suspended", "doesn't exist"]):
                        return {"username": username, "found": False,
                                "reason": "Account not found or suspended"}
                except Exception:
                    pass
                return {"username": username, "display_name": None, "found": True}
        except Exception as exc:
            return {"username": username, "found": False, "reason": str(exc)}


# ── Dashboard Window ─────────────────────────────────────────────────────────

class Dashboard(ctk.CTk):
    """Main dashboard window.  Closing it hides to tray instead of quitting."""

    _REFRESH_MS = 2000      # UI refresh interval

    def __init__(self, state: AppState, monitor: MonitorThread) -> None:
        super().__init__()
        self._state   = state
        self._monitor = monitor

        self.title("X Monitor")
        self.geometry("780x620")
        self.minsize(700, 540)
        self.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_ui()
        self.after(self._REFRESH_MS, self._refresh)

    # ── Build layout ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_stats()
        self._build_tabs()

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, height=64, fg_color=_BG_CARD, corner_radius=0)
        hdr.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 2))
        hdr.grid_columnconfigure(1, weight=1)

        # Status dot + label
        self._dot_lbl = ctk.CTkLabel(
            hdr, text="●", font=ctk.CTkFont(size=22), text_color=_GREEN
        )
        self._dot_lbl.grid(row=0, column=0, padx=(18, 6), pady=16)

        self._status_lbl = ctk.CTkLabel(
            hdr, text="LIVE", font=ctk.CTkFont(size=14, weight="bold"),
            text_color=_GREEN,
        )
        self._status_lbl.grid(row=0, column=1, sticky="w")

        # Uptime
        self._uptime_lbl = ctk.CTkLabel(
            hdr, text="Uptime: --", text_color=_GRAY,
            font=ctk.CTkFont(size=12),
        )
        self._uptime_lbl.grid(row=0, column=2, padx=20)

        # Stop button
        self._stop_btn = ctk.CTkButton(
            hdr, text="⏹  Stop", width=100, height=32,
            fg_color=_RED, hover_color="#c0392b",
            command=self._on_stop,
        )
        self._stop_btn.grid(row=0, column=3, padx=18)

    def _build_stats(self) -> None:
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        for i in range(4):
            row.grid_columnconfigure(i, weight=1)

        labels   = ["Checks", "New Posts", "Errors", "Workers"]
        colors   = [_X_BLUE, _GREEN, _RED, _GRAY]
        self._stat_vals: list[ctk.CTkLabel] = []

        for i, (lbl, col) in enumerate(zip(labels, colors)):
            card = ctk.CTkFrame(row, fg_color=_BG_CARD, corner_radius=10)
            card.grid(row=0, column=i, sticky="ew", padx=4)
            card.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                card, text=lbl, font=ctk.CTkFont(size=11),
                text_color=_GRAY,
            ).grid(row=0, column=0, pady=(10, 0))

            val = ctk.CTkLabel(
                card, text="0", font=ctk.CTkFont(size=26, weight="bold"),
                text_color=col,
            )
            val.grid(row=1, column=0, pady=(0, 10))
            self._stat_vals.append(val)

    def _build_tabs(self) -> None:
        tabs = ctk.CTkTabview(self, fg_color=_BG_CARD)
        tabs.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        tabs.add("Detections")
        tabs.add("Users")
        tabs.add("Settings")

        self._build_detections_tab(tabs.tab("Detections"))
        self._build_users_tab(tabs.tab("Users"))
        self._build_settings_tab(tabs.tab("Settings"))

    # ── Detections tab ───────────────────────────────────────────────────

    def _build_detections_tab(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        self._det_scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self._det_scroll.grid(row=0, column=0, sticky="nsew")
        self._det_scroll.grid_columnconfigure(0, weight=1)
        self._det_rows: list[ctk.CTkFrame] = []

    def _refresh_detections(self) -> None:
        detections = self._state.get_detections_snapshot()

        # Add only rows that are new (prepend, keep list in sync)
        needed = len(detections)
        existing = len(self._det_rows)

        # Rebuild if list changed size
        if needed != existing:
            for w in self._det_rows:
                w.destroy()
            self._det_rows.clear()

            for det in detections:
                row = self._make_detection_row(self._det_scroll, det)
                row.grid(sticky="ew", pady=3)
                self._det_rows.append(row)
        else:
            # Update existing rows in-place
            for i, det in enumerate(detections):
                self._update_detection_row(self._det_rows[i], det)

    def _make_detection_row(self, parent: ctk.CTkFrame, det) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="#1e2028", corner_radius=8)
        frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="🔔", font=ctk.CTkFont(size=18)).grid(
            row=0, column=0, rowspan=2, padx=12, pady=10
        )
        name = det.display_name or f"@{det.username}"
        age  = _time_ago(det.detected_at)

        ctk.CTkLabel(
            frame, text=f"{name}  (@{det.username})",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=4, pady=(8, 0))

        url_btn = ctk.CTkButton(
            frame,
            text=det.post_url[:60] + "…" if len(det.post_url) > 60 else det.post_url,
            font=ctk.CTkFont(size=11), text_color=_X_BLUE,
            fg_color="transparent", hover_color="#1e2028",
            anchor="w", cursor="hand2",
            command=lambda u=det.post_url: webbrowser.open(u),
        )
        url_btn.grid(row=1, column=1, sticky="w", padx=4, pady=(0, 6))

        ctk.CTkLabel(
            frame, text=age, font=ctk.CTkFont(size=11),
            text_color=_GRAY,
        ).grid(row=0, column=2, padx=12, pady=(8, 0))

        return frame

    def _update_detection_row(self, frame: ctk.CTkFrame, det) -> None:
        # Light update — just refresh the time label (3rd label child)
        children = frame.winfo_children()
        for child in children:
            if isinstance(child, ctk.CTkLabel) and child.cget("text_color") == _GRAY:
                child.configure(text=_time_ago(det.detected_at))
                break

    # ── Users tab ────────────────────────────────────────────────────────

    def _build_users_tab(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        self._usr_scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self._usr_scroll.grid(row=0, column=0, sticky="nsew")
        for col in range(4):
            self._usr_scroll.grid_columnconfigure(col, weight=1)
        self._usr_cards: dict[str, ctk.CTkFrame] = {}

    def _refresh_users(self) -> None:
        statuses = self._state.get_user_statuses_snapshot()
        status_map = {s.username.lower(): s for s in statuses}

        # Show all configured users, even unseen ones (use live AppState list)
        all_users = [u.lower() for u in self._state.get_monitored_users()]

        for i, uname in enumerate(all_users):
            col = i % 4
            row = i // 4
            status = status_map.get(uname)

            if uname not in self._usr_cards:
                card = self._make_user_card(self._usr_scroll, uname, status)
                card.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")
                self._usr_cards[uname] = card
            else:
                self._update_user_card(self._usr_cards[uname], status)

    def _make_user_card(self, parent, uname: str, status) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=_BG_CARD, corner_radius=10, width=160)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text=f"@{uname}",
            font=ctk.CTkFont(size=12, weight="bold"),
            wraplength=140, anchor="center",
        ).grid(row=0, column=0, padx=8, pady=(10, 4), sticky="ew")

        dot_color = _GREEN if (status and status.ok) else (_GRAY if not status else _RED)
        dot = ctk.CTkLabel(card, text="●", font=ctk.CTkFont(size=14), text_color=dot_color)
        dot.grid(row=1, column=0, pady=(0, 4))
        card._dot = dot  # type: ignore[attr-defined]

        time_text = _time_ago(status.last_checked) if status and status.last_checked else "Pending…"
        time_lbl = ctk.CTkLabel(
            card, text=time_text, font=ctk.CTkFont(size=10),
            text_color=_GRAY,
        )
        time_lbl.grid(row=2, column=0, pady=(0, 10))
        card._time_lbl = time_lbl  # type: ignore[attr-defined]

        return card

    def _update_user_card(self, card: ctk.CTkFrame, status) -> None:
        if not status:
            return
        dot_color  = _GREEN if status.ok else _RED
        time_text  = _time_ago(status.last_checked) if status.last_checked else "Pending…"
        card._dot.configure(text_color=dot_color)        # type: ignore[attr-defined]
        card._time_lbl.configure(text=time_text)         # type: ignore[attr-defined]

    # ── Settings tab ─────────────────────────────────────────────────────

    def _build_settings_tab(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(1, weight=1)

        def row_lbl(r: int, text: str) -> None:
            ctk.CTkLabel(
                parent, text=text, anchor="w",
                font=ctk.CTkFont(size=13),
            ).grid(row=r, column=0, padx=20, pady=12, sticky="w")

        # Check interval
        row_lbl(0, "Check interval (seconds)")
        self._interval_var = ctk.StringVar(value=str(config.CHECK_INTERVAL_SECONDS))
        ctk.CTkEntry(parent, textvariable=self._interval_var, width=80).grid(
            row=0, column=1, sticky="w", padx=10
        )

        # Workers
        row_lbl(1, "Worker count")
        self._workers_var = ctk.StringVar(value=str(config.WORKER_COUNT))
        ctk.CTkEntry(parent, textvariable=self._workers_var, width=80).grid(
            row=1, column=1, sticky="w", padx=10
        )

        # Headless toggle
        row_lbl(2, "Headless browser")
        self._headless_var = ctk.BooleanVar(value=config.HEADLESS)
        ctk.CTkSwitch(parent, variable=self._headless_var, text="").grid(
            row=2, column=1, sticky="w", padx=10
        )

        # Notification sound
        row_lbl(3, "Notification sound")
        self._sound_var = ctk.BooleanVar(value=config.NOTIFICATION_SOUND)
        ctk.CTkSwitch(parent, variable=self._sound_var, text="").grid(
            row=3, column=1, sticky="w", padx=10
        )

        # Separator
        ctk.CTkFrame(parent, height=1, fg_color=_GRAY).grid(
            row=4, column=0, columnspan=3, sticky="ew", padx=20, pady=8
        )

        # Monitored users list
        row_lbl(5, "Monitored users")
        self._users_text = ctk.CTkTextbox(parent, height=120, width=360)
        self._users_text.grid(row=5, column=1, columnspan=2, padx=10, pady=6, sticky="w")
        self._users_text.insert("0.0", "\n".join(config.MONITORED_USERS))

        # Save button
        ctk.CTkButton(
            parent, text="Save & Restart", command=self._on_save_settings,
            fg_color=_X_BLUE, hover_color="#1a8cd8",
        ).grid(row=6, column=1, sticky="w", padx=10, pady=12)

    def _on_save_settings(self) -> None:
        try:
            interval = int(self._interval_var.get())
            workers  = int(self._workers_var.get())
            users_raw = self._users_text.get("0.0", "end").strip()
            users = [u.strip() for u in users_raw.splitlines() if u.strip()]

            config.CHECK_INTERVAL_SECONDS = interval  # type: ignore[misc]
            config.WORKER_COUNT           = workers    # type: ignore[misc]
            config.HEADLESS               = self._headless_var.get()   # type: ignore[misc]
            config.NOTIFICATION_SOUND     = self._sound_var.get()       # type: ignore[misc]
            config.MONITORED_USERS        = users      # type: ignore[misc]

            # Clear user cards so they rebuild on next refresh
            for card in self._usr_cards.values():
                card.destroy()
            self._usr_cards.clear()

        except ValueError:
            pass  # invalid input — ignore silently

    # ── Periodic refresh ─────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._refresh_header()
        self._refresh_stats()
        self._refresh_detections()
        self._refresh_users()
        self.after(self._REFRESH_MS, self._refresh)

    def _refresh_header(self) -> None:
        if self._state.is_monitoring:
            self._dot_lbl.configure(text_color=_GREEN)
            self._status_lbl.configure(text="LIVE", text_color=_GREEN)
            self._stop_btn.configure(state="normal")
        else:
            self._dot_lbl.configure(text_color=_RED)
            self._status_lbl.configure(text="STOPPED", text_color=_RED)
            self._stop_btn.configure(state="disabled")

        if self._state.started_at:
            self._uptime_lbl.configure(text=f"Uptime: {_uptime(self._state.started_at)}")

    def _refresh_stats(self) -> None:
        self._stat_vals[0].configure(text=str(self._state.checked_count))
        self._stat_vals[1].configure(text=str(self._state.new_posts_count))
        self._stat_vals[2].configure(text=str(self._state.error_count))
        self._stat_vals[3].configure(text=str(config.WORKER_COUNT))

    # ── Controls ─────────────────────────────────────────────────────────

    def _on_stop(self) -> None:
        self._monitor.stop()

    # ── Visibility ───────────────────────────────────────────────────────

    def show(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self) -> None:
        self.withdraw()


# ── System tray ──────────────────────────────────────────────────────────────

class TrayApp:
    """Wraps a pystray.Icon and runs it in a background thread."""

    def __init__(
        self,
        state: AppState,
        monitor: MonitorThread,
        cmd_queue: "queue.Queue[str]",
    ) -> None:
        self._state    = state
        self._monitor  = monitor
        self._queue    = cmd_queue
        self._icon: Optional[pystray.Icon] = None

    def start(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", self._on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Stop Monitoring",  self._on_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit",             self._on_exit),
        )
        self._icon = pystray.Icon(
            "xmonitor",
            _make_tray_icon(active=True),
            "X Monitor",
            menu,
        )
        t = threading.Thread(target=self._icon.run, daemon=True, name="tray-thread")
        t.start()

    def _on_open(self, *_: object) -> None:
        self._queue.put("show")

    def _on_stop(self, *_: object) -> None:
        self._monitor.stop()

    def _on_exit(self, *_: object) -> None:
        self._monitor.stop()
        self._queue.put("exit")

    def update_icon(self, active: bool) -> None:
        if self._icon:
            self._icon.icon = _make_tray_icon(active)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _uptime(started_at: datetime) -> str:
    delta = datetime.now(timezone.utc) - started_at
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"


def _time_ago(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    delta = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else datetime.now(timezone.utc) - dt
    secs  = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> None:
    """Launch the full application: monitoring thread + tray + dashboard."""

    log_setup.setup_logging()

    state    = AppState()
    notifier = ToastNotifier()
    cmd_q: queue.Queue[str] = queue.Queue()

    # Start monitoring in background
    monitor = MonitorThread(state, notifier)
    monitor.start()

    # Start tray icon in background
    tray = TrayApp(state, monitor, cmd_q)
    tray.start()

    # Build dashboard (hidden initially — appears on tray click)
    dashboard = Dashboard(state, monitor)
    dashboard.withdraw()   # start hidden in tray

    # Process tray commands in the tkinter thread
    def _poll_queue() -> None:
        while not cmd_q.empty():
            cmd = cmd_q.get_nowait()
            if cmd == "show":
                dashboard.show()
            elif cmd == "exit":
                monitor.stop()
                dashboard.destroy()
                return
        # Also sync tray icon colour with monitoring state
        tray.update_icon(state.is_monitoring)
        dashboard.after(500, _poll_queue)

    dashboard.after(500, _poll_queue)
    dashboard.mainloop()


if __name__ == "__main__":
    run()
