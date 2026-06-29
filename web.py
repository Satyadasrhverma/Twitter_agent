"""
Web dashboard server — http://localhost:8080
FastAPI backend + single-page HTML/CSS/JS frontend.
"""

import asyncio
import re as _re
import sqlite3
import threading
import urllib.request as _urllib
import webbrowser
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import auth
import config
from ui_state import AppState

# ── Globals set by start_server() ──────────────────────────────────────────
_state:   Optional[AppState]  = None
_monitor: Optional[Any]       = None   # MonitorThread | None

app = FastAPI(title="X Monitor", docs_url=None, redoc_url=None)
PORT = 8080


# ── REST API ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _HTML


@app.get("/api/status")
async def get_status() -> JSONResponse:
    if _state is None:
        return JSONResponse({"error": "not ready"}, status_code=503)
    s = _state
    now = datetime.now(timezone.utc)

    uptime = "--"
    if s.started_at:
        d = int((now - s.started_at).total_seconds())
        h, r = divmod(d, 3600); m, sc = divmod(r, 60)
        uptime = f"{h:02d}h {m:02d}m {sc:02d}s"

    detections = [
        {
            "username":     d.username,
            "display_name": d.display_name or f"@{d.username}",
            "post_url":     d.post_url,
            "time_ago":     _ago(d.detected_at),
            "ts":           d.detected_at.isoformat(),
        }
        for d in s.get_detections_snapshot()
    ]

    smap = {u.username.lower(): u for u in s.get_user_statuses_snapshot()}
    monitored = s.get_monitored_users()
    users = [
        {
            "username":     u,
            "display_name": s.get_display_name(u),
            "ok":           smap[u.lower()].ok           if u.lower() in smap else None,
            "last_checked": _ago(smap[u.lower()].last_checked) if u.lower() in smap else "Pending",
            "post_id":      smap[u.lower()].last_post_id if u.lower() in smap else None,
        }
        for u in monitored
    ]

    return JSONResponse({
        "is_monitoring":   s.is_monitoring,
        "uptime":          uptime,
        "checked_count":   s.checked_count,
        "new_posts_count": s.new_posts_count,
        "error_count":     s.error_count,
        "worker_count":    config.WORKER_COUNT,
        "user_count":      len(monitored),
        "max_users":       config.MAX_MONITORED_USERS,
        "detections":      detections,
        "users":           users,
    })


@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    users = _state.get_monitored_users() if _state else config.MONITORED_USERS
    return JSONResponse({
        "interval":  config.CHECK_INTERVAL_SECONDS,
        "workers":   config.WORKER_COUNT,
        "headless":  config.HEADLESS,
        "sound":     config.NOTIFICATION_SOUND,
        "users":     users,
        "max_users": config.MAX_MONITORED_USERS,
    })


class SettingsIn(BaseModel):
    interval: int
    workers:  int
    headless: bool
    sound:    bool


@app.post("/api/settings")
async def save_settings(body: SettingsIn) -> JSONResponse:
    config.CHECK_INTERVAL_SECONDS = body.interval   # type: ignore[misc]
    config.WORKER_COUNT           = body.workers    # type: ignore[misc]
    config.HEADLESS               = body.headless   # type: ignore[misc]
    config.NOTIFICATION_SOUND     = body.sound      # type: ignore[misc]
    return JSONResponse({"ok": True})


# ── User management endpoints ────────────────────────────────────────────────

class UsernameIn(BaseModel):
    username: str


class AddUserIn(BaseModel):
    username:         str
    display_name:     Optional[str] = None
    initial_post_id:  Optional[str] = None
    initial_post_url: Optional[str] = None


_TITLE_PAT = _re.compile(r'<title[^>]*>(.+?)</title>', _re.I | _re.S)
_NAME_PAT  = _re.compile(r'^(.+?)\s*\(@[^)]+\)')


async def _quick_search(username: str) -> Optional[dict]:
    """Fast urllib lookup — reads just the page <title> (~2-3 sec, no browser)."""
    def _fetch():
        url = f"https://x.com/{username}"
        req = _urllib.Request(url, headers={
            "User-Agent": config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        })
        try:
            with _urllib.urlopen(req, timeout=8) as resp:
                html = resp.read(8192).decode("utf-8", errors="ignore")
            m = _TITLE_PAT.search(html)
            if not m:
                return None
            title = m.group(1).strip()
            nm = _NAME_PAT.match(title)
            if nm:
                return {"username": username, "display_name": nm.group(1).strip(), "found": True}
            lower = html.lower()
            if "account suspended" in lower or "doesn't exist" in lower:
                return {"username": username, "found": False, "reason": "Account not found or suspended"}
            return None  # title didn't match pattern — fall back to browser
        except Exception:
            return None  # network error — fall back to browser
    return await asyncio.to_thread(_fetch)


@app.post("/api/users/search")
async def search_user_endpoint(body: UsernameIn) -> JSONResponse:
    try:
        query = body.username.strip().lstrip("@")
        if not query:
            return JSONResponse({"found": False, "reason": "Empty username"})

        # ── Multi-result name search (logged-in session required) ──────────────
        has_space = " " in query
        if auth.session_exists() and _monitor is not None and has_space:
            try:
                results = await asyncio.to_thread(_monitor.search_users_by_name, query)
                if results:
                    return JSONResponse({"found": True, "multiple": True, "results": results})
            except Exception:
                pass

        # ── Single-user lookup — collect result then augment with latest post ──
        result: Optional[dict] = None
        try:
            result = await _quick_search(query)
        except Exception:
            pass

        if result is None:
            if _monitor is None:
                return JSONResponse({"found": False, "reason": "Monitor not ready — try again in a moment"})
            try:
                result = await asyncio.to_thread(_monitor.search_user, query)
            except Exception as exc:
                return JSONResponse({"found": False, "reason": f"Search failed: {exc}"})

        if not result:
            return JSONResponse({"found": False, "reason": "User not found"})

        # ── Augment with latest tweet ID (fast API path, logged-in only) ───────
        if result.get("found") and _monitor is not None:
            try:
                post = await asyncio.to_thread(_monitor.get_latest_post, result["username"])
                if post:
                    result["latest_post_id"]  = post["post_id"]
                    result["latest_post_url"] = post["post_url"]
            except Exception:
                pass

        return JSONResponse(result)

    except Exception as exc:
        return JSONResponse({"found": False, "reason": str(exc)})


def _seed_tweet_sync(username: str, post_id: str, post_url: str) -> None:
    """Write initial tweet ID to DB using sync sqlite (safe from any thread)."""
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=5)
        conn.execute(
            """INSERT INTO monitored_users (username, latest_post_id, latest_post_url)
               VALUES (?, ?, ?) ON CONFLICT(username) DO NOTHING""",
            (username.lower(), post_id, post_url),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("Could not seed tweet ID for @%s: %s", username, exc)


@app.post("/api/users/add")
async def add_user_endpoint(body: AddUserIn) -> JSONResponse:
    if _state is None:
        return JSONResponse({"ok": False, "reason": "Not ready"})
    ok, reason = _state.add_user(body.username, body.display_name)
    if ok and body.initial_post_id:
        await asyncio.to_thread(
            _seed_tweet_sync,
            body.username,
            body.initial_post_id,
            body.initial_post_url or "",
        )
    return JSONResponse({
        "ok":         ok,
        "reason":     reason,
        "user_count": len(_state.get_monitored_users()),
        "max_users":  config.MAX_MONITORED_USERS,
    })


@app.delete("/api/users/{username}")
async def remove_user_endpoint(username: str) -> JSONResponse:
    if _state is None:
        return JSONResponse({"ok": False})
    removed = _state.remove_user(username)
    return JSONResponse({"ok": removed, "user_count": len(_state.get_monitored_users())})


# ── Twitter auth endpoints ───────────────────────────────────────────────────

@app.get("/api/auth/status")
async def auth_status() -> JSONResponse:
    login_st = auth.get_login_state()
    return JSONResponse({
        "logged_in":        auth.session_exists(),
        "username":         auth.get_session_username(),
        "login_in_progress": login_st["in_progress"],
        "login_done":       login_st["done"],
        "login_result":     login_st["result"],
    })


@app.post("/api/auth/login")
async def auth_login() -> JSONResponse:
    started, msg = auth.start_login()
    return JSONResponse({"started": started, "message": msg})


@app.post("/api/auth/logout")
async def auth_logout() -> JSONResponse:
    auth.clear_session()
    return JSONResponse({"ok": True})


@app.post("/api/auth/cancel")
async def auth_cancel() -> JSONResponse:
    """Cancel an in-progress login attempt."""
    auth._set_state(in_progress=False, done=False, result=None)
    return JSONResponse({"ok": True})


class ManualCookieIn(BaseModel):
    auth_token: str
    ct0:        str


@app.post("/api/auth/manual")
async def auth_manual(body: ManualCookieIn) -> JSONResponse:
    ok, result = await asyncio.to_thread(auth.save_manual_cookies, body.auth_token, body.ct0)
    if ok:
        return JSONResponse({"ok": True, "username": result})
    return JSONResponse({"ok": False, "reason": result})


@app.post("/api/auth/import-browser")
async def auth_import_browser() -> JSONResponse:
    """Import Twitter session directly from Edge/Chrome browser cookies."""
    ok, reason = await asyncio.to_thread(auth.import_from_browser)
    return JSONResponse({"ok": ok, "reason": reason})


class ControlIn(BaseModel):
    action: str   # "stop"


@app.post("/api/control")
async def control(body: ControlIn) -> JSONResponse:
    if body.action == "stop" and _monitor is not None:
        _monitor.stop()
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "reason": "unknown action"})


# ── Server lifecycle ────────────────────────────────────────────────────────

def start_server(state: AppState, monitor: Any, open_browser: bool = True) -> threading.Thread:
    global _state, _monitor
    _state   = state
    _monitor = monitor

    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT,
                         log_level="error", loop="asyncio")
    server = uvicorn.Server(cfg)

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, daemon=True, name="web-server")
    t.start()

    if open_browser:
        threading.Timer(1.8, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    return t


# ── Helpers ─────────────────────────────────────────────────────────────────

def _ago(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    s = int((datetime.now(timezone.utc) - aware).total_seconds())
    if s < 60:    return f"{s}s ago"
    if s < 3600:  return f"{s//60}m ago"
    if s < 86400: return f"{s//3600}h ago"
    return f"{s//86400}d ago"


# ── Single-page HTML/CSS/JS ─────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>X Monitor</title>
<style>
/* ── Reset & Variables ─────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }
:root {
  --bg:         #000000;
  --surface:    #0f1117;
  --card:       #16181c;
  --border:     #2f3336;
  --blue:       #1d9bf0;
  --blue-dim:   #1a8cd8;
  --green:      #00ba7c;
  --red:        #f4212e;
  --orange:     #ff7043;
  --gray:       #71767b;
  --gray-light: #9ca3af;
  --white:      #e7e9ea;
  --font:       -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --sidebar-w:  240px;
  --radius:     12px;
  --transition: .18s ease;
}
html, body { height: 100%; background: var(--bg); color: var(--white); font-family: var(--font) }
a { color: inherit; text-decoration: none }
button { cursor: pointer; font-family: var(--font); border: none; outline: none }
input, textarea, select { font-family: var(--font); background: var(--surface); color: var(--white); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; outline: none; transition: border-color var(--transition) }
input:focus, textarea:focus { border-color: var(--blue) }
textarea { resize: vertical; line-height: 1.6 }

/* ── Layout ────────────────────────────────────────────────────────────── */
.app { display: flex; height: 100vh; overflow: hidden }

/* ── Sidebar ───────────────────────────────────────────────────────────── */
.sidebar {
  width: var(--sidebar-w);
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
  flex-shrink: 0;
  padding: 20px 0;
}
.sidebar-logo {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 20px 24px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
}
.logo-icon {
  width: 40px; height: 40px; border-radius: 50%;
  background: var(--blue); display: grid; place-items: center;
  font-size: 22px; font-weight: 900; color: #fff; flex-shrink: 0;
}
.logo-text { font-size: 18px; font-weight: 800; letter-spacing: -.3px }
.logo-sub  { font-size: 11px; color: var(--gray); margin-top: 1px }

.nav-item {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 20px; margin: 2px 10px;
  border-radius: 10px; cursor: pointer;
  font-size: 15px; font-weight: 500; color: var(--gray-light);
  transition: all var(--transition);
}
.nav-item:hover  { background: rgba(255,255,255,.06); color: var(--white) }
.nav-item.active { background: rgba(29,155,240,.12); color: var(--blue); font-weight: 700 }
.nav-icon { font-size: 18px; width: 22px; text-align: center }

.sidebar-footer {
  margin-top: auto; padding: 16px 20px;
  border-top: 1px solid var(--border);
}
.status-pill {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-radius: 20px;
  background: var(--card); font-size: 13px;
}
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0 }
.status-dot.live { background: var(--green); box-shadow: 0 0 6px var(--green) }
.status-dot.down { background: var(--red) }
.status-dot.pulse { animation: pulse 2s infinite }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

/* ── Main area ─────────────────────────────────────────────────────────── */
.main { flex: 1; overflow-y: auto; display: flex; flex-direction: column }

.topbar {
  display: flex; align-items: center; gap: 12px;
  padding: 16px 28px;
  border-bottom: 1px solid var(--border);
  background: rgba(0,0,0,.85);
  backdrop-filter: blur(10px);
  position: sticky; top: 0; z-index: 10;
}
.page-title { font-size: 20px; font-weight: 800; flex: 1 }
.uptime-lbl { font-size: 12px; color: var(--gray); padding: 6px 12px; background: var(--card); border-radius: 20px }

.btn {
  padding: 8px 18px; border-radius: 20px;
  font-size: 14px; font-weight: 700;
  transition: all var(--transition);
}
.btn-stop  { background: rgba(244,33,46,.15); color: var(--red); border: 1px solid rgba(244,33,46,.3) }
.btn-stop:hover  { background: rgba(244,33,46,.25) }
.btn-blue  { background: var(--blue); color: #fff }
.btn-blue:hover  { background: var(--blue-dim) }
.btn-ghost { background: transparent; color: var(--gray-light); border: 1px solid var(--border) }
.btn-ghost:hover { background: var(--card); color: var(--white) }
.btn:disabled { opacity: .4; cursor: not-allowed }

/* ── Pages ─────────────────────────────────────────────────────────────── */
.page { display: none; padding: 28px; flex: 1 }
.page.active { display: block }

/* ── Stat cards ────────────────────────────────────────────────────────── */
.stats-grid {
  display: grid; grid-template-columns: repeat(4,1fr); gap: 14px;
  margin-bottom: 28px;
}
.stat-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 20px 22px;
  position: relative; overflow: hidden;
}
.stat-card::before {
  content: ''; position: absolute; inset: 0;
  border-radius: var(--radius); opacity: .06;
}
.stat-card.blue::before  { background: var(--blue) }
.stat-card.green::before { background: var(--green) }
.stat-card.red::before   { background: var(--red) }
.stat-card.gray::before  { background: var(--gray) }
.stat-label { font-size: 11px; color: var(--gray); letter-spacing: .08em; text-transform: uppercase; margin-bottom: 10px }
.stat-value { font-size: 38px; font-weight: 900; line-height: 1 }
.stat-card.blue  .stat-value { color: var(--blue) }
.stat-card.green .stat-value { color: var(--green) }
.stat-card.red   .stat-value { color: var(--red) }
.stat-card.gray  .stat-value { color: var(--gray-light) }
.stat-icon { position: absolute; right: 18px; top: 16px; font-size: 26px; opacity: .2 }

/* ── Two-col layout ────────────────────────────────────────────────────── */
.two-col { display: grid; grid-template-columns: 3fr 2fr; gap: 20px; align-items: start }

/* ── Section card ──────────────────────────────────────────────────────── */
.section {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); overflow: hidden;
}
.section-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
}
.section-title { font-size: 13px; font-weight: 700; color: var(--gray); letter-spacing: .06em; text-transform: uppercase }
.section-badge {
  font-size: 11px; padding: 2px 8px; border-radius: 20px;
  background: rgba(29,155,240,.15); color: var(--blue); font-weight: 600;
}

/* ── Detection feed ────────────────────────────────────────────────────── */
.feed { max-height: 420px; overflow-y: auto }
.det-row {
  display: flex; align-items: flex-start; gap: 14px;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
  transition: background var(--transition);
}
.det-row:last-child { border-bottom: none }
.det-row:hover { background: rgba(255,255,255,.03) }
.det-row.fresh { animation: fadein .5s ease }
@keyframes fadein { from{opacity:0;transform:translateY(-6px)} to{opacity:1;transform:none} }
.det-bell { font-size: 22px; margin-top: 1px; flex-shrink: 0 }
.det-body { flex: 1; min-width: 0 }
.det-name { font-size: 14px; font-weight: 700; margin-bottom: 4px }
.det-handle { color: var(--gray); font-weight: 400; font-size: 13px }
.det-url {
  display: block; font-size: 12px; color: var(--blue);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  transition: color var(--transition);
}
.det-url:hover { color: var(--blue-dim); text-decoration: underline }
.det-time { font-size: 12px; color: var(--gray); white-space: nowrap; margin-top: 3px }
.empty-feed { padding: 40px 20px; text-align: center; color: var(--gray); font-size: 14px }
.empty-feed .e-icon { font-size: 32px; margin-bottom: 10px }

/* ── Users grid ────────────────────────────────────────────────────────── */
.user-grid-wrap { padding: 14px }
.user-grid { display: grid; grid-template-columns: repeat(2,1fr); gap: 8px }
.user-card {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: 8px;
  background: var(--surface); border: 1px solid var(--border);
  transition: border-color var(--transition);
}
.user-card:hover { border-color: var(--blue) }
.u-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0 }
.u-dot.ok      { background: var(--green) }
.u-dot.err     { background: var(--red) }
.u-dot.pending { background: var(--gray) }
.u-info { min-width: 0 }
.u-handle { font-size: 12px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis }
.u-time   { font-size: 10px; color: var(--gray) }

/* ── Users page (full table) ───────────────────────────────────────────── */
.user-table { width: 100%; border-collapse: collapse }
.user-table th {
  padding: 12px 18px; text-align: left;
  font-size: 11px; font-weight: 700; color: var(--gray);
  letter-spacing: .08em; text-transform: uppercase;
  border-bottom: 1px solid var(--border);
}
.user-table td { padding: 14px 18px; border-bottom: 1px solid var(--border); font-size: 14px }
.user-table tr:last-child td { border-bottom: none }
.user-table tr:hover td { background: rgba(255,255,255,.02) }
.badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 600 }
.badge.ok      { background: rgba(0,186,124,.15); color: var(--green) }
.badge.err     { background: rgba(244,33,46,.15);  color: var(--red) }
.badge.pending { background: rgba(113,118,123,.15);color: var(--gray) }
.post-link { color: var(--blue); font-size: 12px; max-width: 280px; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.post-link:hover { text-decoration: underline }

/* ── Settings page ─────────────────────────────────────────────────────── */
.settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; max-width: 860px }
.settings-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 22px;
}
.settings-card h3 { font-size: 15px; font-weight: 700; margin-bottom: 18px; padding-bottom: 12px; border-bottom: 1px solid var(--border); display:flex; align-items:center; gap:8px }
.form-row { margin-bottom: 16px }
.form-label { display: block; font-size: 13px; color: var(--gray-light); margin-bottom: 6px }
.form-row input { width: 100% }
.toggle-row { display: flex; align-items: center; justify-content: space-between; padding: 10px 0 }
.toggle-row label { font-size: 14px }
.toggle { position: relative; width: 44px; height: 24px; flex-shrink: 0 }
.toggle input { opacity: 0; width: 0; height: 0 }
.toggle-slider {
  position: absolute; inset: 0; background: var(--border);
  border-radius: 24px; transition: .25s; cursor: pointer;
}
.toggle-slider::before {
  content: ''; position: absolute;
  width: 18px; height: 18px; left: 3px; top: 3px;
  background: #fff; border-radius: 50%; transition: .25s;
}
.toggle input:checked + .toggle-slider { background: var(--blue) }
.toggle input:checked + .toggle-slider::before { transform: translateX(20px) }
.settings-save-row { margin-top: 20px; display: flex; gap: 10px; align-items: center }
.save-msg { font-size: 13px; color: var(--green); opacity: 0; transition: opacity .3s }
.save-msg.show { opacity: 1 }
.settings-full { grid-column: 1 / -1 }

/* ── Search & Manage Users ─────────────────────────────────────────────── */
.search-bar { display:flex; align-items:center; gap:10px; margin-bottom:14px }
.search-at  { color:var(--gray); font-size:20px; font-weight:800; flex-shrink:0 }
.search-bar input { flex:1; font-size:14px }
.count-pill { font-size:12px; padding:3px 10px; border-radius:20px; font-weight:700 }
.count-pill.ok   { background:rgba(29,155,240,.15); color:var(--blue) }
.count-pill.warn { background:rgba(255,112,67,.15);  color:var(--orange) }
.count-pill.full { background:rgba(244,33,46,.15);   color:var(--red) }
.limit-bar { height:3px; background:var(--border); border-radius:3px; margin-bottom:18px; overflow:hidden }
.limit-fill { height:100%; border-radius:3px; background:var(--blue); transition:width .4s ease }
.limit-fill.warn { background:var(--orange) }
.limit-fill.full { background:var(--red) }
.result-wrap { margin-bottom:14px }
.result-card { display:flex; align-items:center; gap:14px; padding:14px 16px; border-radius:10px; background:var(--surface); border:1px solid var(--border); animation:fadein .25s ease }
.result-avatar { width:44px; height:44px; border-radius:50%; background:var(--blue); display:grid; place-items:center; font-size:18px; font-weight:800; color:#fff; flex-shrink:0 }
.result-info { flex:1; min-width:0 }
.result-name   { font-size:15px; font-weight:700 }
.result-handle { font-size:13px; color:var(--gray) }
.result-error  { display:flex; align-items:center; gap:10px; color:var(--red); padding:12px 4px; font-size:14px }
.already-tag { font-size:12px; color:var(--green); background:rgba(0,186,124,.12); padding:4px 12px; border-radius:20px; flex-shrink:0 }
.limit-tag   { font-size:12px; color:var(--red);   background:rgba(244,33,46,.12);  padding:4px 12px; border-radius:20px; flex-shrink:0 }
.managed-row { display:flex; align-items:center; gap:12px; padding:11px 18px; border-top:1px solid var(--border); transition:background var(--transition) }
.managed-row:hover { background:rgba(255,255,255,.025) }
.managed-avatar { width:36px; height:36px; border-radius:50%; background:var(--surface); display:grid; place-items:center; font-size:14px; font-weight:800; flex-shrink:0; border:1px solid var(--border) }
.managed-info   { flex:1; min-width:0 }
.managed-dname  { font-size:13px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
.managed-meta   { font-size:11px; color:var(--gray); margin-top:2px }
.btn-remove { width:28px; height:28px; border-radius:50%; background:transparent; color:var(--gray); border:1px solid var(--border); font-size:13px; display:grid; place-items:center; flex-shrink:0; cursor:pointer; transition:all var(--transition) }
.btn-remove:hover { background:rgba(244,33,46,.15); color:var(--red); border-color:rgba(244,33,46,.4) }
.btn-sm { padding:6px 16px; font-size:13px }
.empty-managed { padding:32px 20px; text-align:center; color:var(--gray); font-size:14px }
.spinner { display:inline-block; width:13px; height:13px; border:2px solid rgba(255,255,255,.3); border-top-color:#fff; border-radius:50%; animation:spin .6s linear infinite; vertical-align:middle }
@keyframes spin { to { transform:rotate(360deg) } }

/* ── Auth card ─────────────────────────────────────────────────────────── */
.auth-connected { display:flex; align-items:center; gap:14px }
.auth-avatar { width:46px; height:46px; border-radius:50%; object-fit:cover; flex-shrink:0 }
.auth-info { flex:1 }
.auth-name { font-size:15px; font-weight:700 }
.auth-sub  { font-size:12px; color:var(--gray); margin-top:2px }
.auth-disconnected { display:flex; align-items:center; gap:16px; flex-wrap:wrap }
.auth-desc { font-size:13px; color:var(--gray-light); line-height:1.7 }
.login-progress { display:flex; align-items:center; gap:12px; padding:10px 0; font-size:14px; color:var(--gray-light) }
.multi-results { display:flex; flex-direction:column; gap:8px }
.multi-result-card { display:flex; align-items:center; gap:12px; padding:12px 14px; border-radius:10px; background:var(--surface); border:1px solid var(--border); transition:border-color var(--transition) }
.multi-result-card:hover { border-color:var(--blue) }

/* ── Manual cookie section ─────────────────────────────────────────────── */
.cookie-steps { font-size:12px; color:var(--gray-light); line-height:2; margin-bottom:10px }
.cookie-steps b { color:var(--white) }
.cookie-fields { display:flex; flex-direction:column; gap:8px; margin-bottom:10px }
.cookie-fields input { font-size:12px; font-family:monospace; padding:8px 12px }
.cookie-toggle { font-size:12px; color:var(--blue); cursor:pointer; text-decoration:underline; margin-top:6px; display:inline-block }

/* ── Toast ─────────────────────────────────────────────────────────────── */
#toast-wrap { position: fixed; bottom: 24px; right: 24px; display: flex; flex-direction: column; gap: 10px; z-index: 999 }
.toast {
  display: flex; align-items: flex-start; gap: 12px;
  background: var(--card); border: 1px solid var(--border);
  border-left: 3px solid var(--blue);
  border-radius: 10px; padding: 14px 16px;
  min-width: 300px; max-width: 380px;
  box-shadow: 0 8px 32px rgba(0,0,0,.5);
  animation: slideIn .3s ease;
}
@keyframes slideIn { from{opacity:0;transform:translateX(30px)} to{opacity:1;transform:none} }
.toast-icon { font-size: 20px }
.toast-title { font-size: 13px; font-weight: 700; margin-bottom: 3px }
.toast-body  { font-size: 12px; color: var(--gray-light) }
.toast-url   { font-size: 11px; color: var(--blue); word-break: break-all; cursor: pointer }
.toast-url:hover { text-decoration: underline }

/* ── Scrollbar ─────────────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 5px; height: 5px }
::-webkit-scrollbar-track { background: transparent }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px }
::-webkit-scrollbar-thumb:hover { background: var(--gray) }

/* ── Responsive ────────────────────────────────────────────────────────── */
@media(max-width:900px) {
  .stats-grid { grid-template-columns: repeat(2,1fr) }
  .two-col    { grid-template-columns: 1fr }
  .settings-grid { grid-template-columns: 1fr }
  .user-grid  { grid-template-columns: 1fr }
}
@media(max-width:620px) {
  .sidebar { width: 60px }
  .logo-text, .logo-sub, .nav-label, .sidebar-footer { display: none }
  .logo-icon { margin: 0 auto }
  .sidebar-logo { padding: 8px 10px 24px; justify-content: center }
  .nav-item { padding: 12px; margin: 2px 6px; justify-content: center }
  .stats-grid { grid-template-columns: 1fr 1fr }
}
</style>
</head>
<body>
<div class="app">

<!-- ── Sidebar ─────────────────────────────────────────────────────────── -->
<nav class="sidebar">
  <div class="sidebar-logo">
    <div class="logo-icon">𝕏</div>
    <div>
      <div class="logo-text">X Monitor</div>
      <div class="logo-sub">Live tracker</div>
    </div>
  </div>

  <div class="nav-item active" onclick="nav('dashboard')">
    <span class="nav-icon">📊</span>
    <span class="nav-label">Dashboard</span>
  </div>
  <div class="nav-item" onclick="nav('users')">
    <span class="nav-icon">👥</span>
    <span class="nav-label">Users</span>
  </div>
  <div class="nav-item" onclick="nav('settings')">
    <span class="nav-icon">⚙️</span>
    <span class="nav-label">Settings</span>
  </div>

  <div class="sidebar-footer">
    <div class="status-pill">
      <div class="status-dot live pulse" id="sb-dot"></div>
      <span id="sb-status" style="font-size:13px;font-weight:600;color:var(--green)">LIVE</span>
    </div>
  </div>
</nav>

<!-- ── Main ────────────────────────────────────────────────────────────── -->
<div class="main">

  <!-- Topbar -->
  <header class="topbar">
    <div class="page-title" id="page-title">Dashboard</div>
    <div class="uptime-lbl">⏱ <span id="hdr-uptime">--</span></div>
    <button class="btn btn-stop" id="btn-stop" onclick="stopMonitor()">⏹ Stop</button>
  </header>

  <!-- ── Dashboard page ────────────────────────────────────────────────── -->
  <div class="page active" id="page-dashboard">

    <div class="stats-grid">
      <div class="stat-card blue">
        <div class="stat-icon">🔍</div>
        <div class="stat-label">Checks Completed</div>
        <div class="stat-value" id="s-checks">0</div>
      </div>
      <div class="stat-card green">
        <div class="stat-icon">🔔</div>
        <div class="stat-label">New Posts</div>
        <div class="stat-value" id="s-posts">0</div>
      </div>
      <div class="stat-card red">
        <div class="stat-icon">⚠️</div>
        <div class="stat-label">Errors</div>
        <div class="stat-value" id="s-errors">0</div>
      </div>
      <div class="stat-card gray">
        <div class="stat-icon">⚡</div>
        <div class="stat-label">Workers Active</div>
        <div class="stat-value" id="s-workers">0</div>
      </div>
    </div>

    <div class="two-col">

      <!-- Detection feed -->
      <div class="section">
        <div class="section-head">
          <span class="section-title">Recent Detections</span>
          <span class="section-badge" id="det-badge">0</span>
        </div>
        <div class="feed" id="det-feed">
          <div class="empty-feed">
            <div class="e-icon">👀</div>
            Monitoring… new posts will appear here.
          </div>
        </div>
      </div>

      <!-- Mini users panel -->
      <div class="section">
        <div class="section-head">
          <span class="section-title">Monitored Users</span>
          <span class="section-badge" id="usr-badge">0</span>
        </div>
        <div class="user-grid-wrap">
          <div class="user-grid" id="mini-user-grid"></div>
        </div>
      </div>

    </div>
  </div><!-- /dashboard -->

  <!-- ── Users page ────────────────────────────────────────────────────── -->
  <div class="page" id="page-users">
    <div class="section">
      <div class="section-head">
        <span class="section-title">All Monitored Users</span>
        <span class="section-badge" id="users-count-badge">0</span>
      </div>
      <table class="user-table">
        <thead>
          <tr>
            <th>Username</th>
            <th>Status</th>
            <th>Last Checked</th>
            <th>Latest Post</th>
          </tr>
        </thead>
        <tbody id="users-tbody"></tbody>
      </table>
    </div>
  </div><!-- /users -->

  <!-- ── Settings page ─────────────────────────────────────────────────── -->
  <div class="page" id="page-settings">

    <!-- Twitter Login -->
    <div class="section" style="margin-bottom:20px">
      <div class="section-head">
        <span class="section-title">Twitter Account</span>
        <span id="auth-badge" class="section-badge" style="background:rgba(113,118,123,.15);color:var(--gray)">Not connected</span>
      </div>
      <!-- Connected state -->
      <div id="auth-connected" style="display:none;padding:18px;align-items:center;gap:14px">
        <div id="auth-info" style="flex:1;font-size:14px;font-weight:600;color:var(--green)"></div>
        <button class="btn btn-ghost btn-sm" onclick="doLogout()">Disconnect</button>
      </div>
      <!-- Not connected — show manual form directly -->
      <div id="auth-form" style="padding:18px">
        <div style="font-size:13px;color:var(--gray-light);margin-bottom:14px;line-height:1.8">
          Edge/Chrome mein <b style="color:var(--white)">x.com</b> pe login karo → <b style="color:var(--white)">F12</b> dabao → <b style="color:var(--white)">Application</b> → <b style="color:var(--white)">Cookies</b> → <b style="color:var(--white)">https://x.com</b> → neeche se copy karo:
        </div>
        <div style="display:flex;flex-direction:column;gap:10px;max-width:560px">
          <div>
            <div style="font-size:11px;color:var(--gray);margin-bottom:4px;font-weight:700;letter-spacing:.05em">AUTH_TOKEN</div>
            <input type="password" id="mc-auth" placeholder="auth_token value yahan paste karo" style="width:100%;font-family:monospace;font-size:12px" autocomplete="off"/>
          </div>
          <div>
            <div style="font-size:11px;color:var(--gray);margin-bottom:4px;font-weight:700;letter-spacing:.05em">CT0</div>
            <input type="text" id="mc-ct0" placeholder="ct0 value yahan paste karo" style="width:100%;font-family:monospace;font-size:12px" autocomplete="off"/>
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-top:4px">
            <button class="btn btn-blue" id="mc-btn" onclick="doManualConnect()">✅ Connect Karo</button>
            <span id="mc-msg" style="font-size:12px;color:var(--gray)"></span>
          </div>
        </div>
      </div>
    </div>

    <!-- User Search & Management -->
    <div class="section" style="margin-bottom:24px">
      <div class="section-head">
        <span class="section-title">Search &amp; Add Users</span>
        <span id="count-pill" class="count-pill ok">0 / 30</span>
      </div>
      <div style="padding:18px 18px 0">
        <div class="search-bar">
          <span class="search-at">@</span>
          <input type="text" id="search-input"
                 placeholder="Enter username — e.g. elonmusk"
                 oninput="onSearchType()"
                 onkeydown="if(event.key==='Enter'){clearTimeout(_st);searchUser();}"
                 maxlength="50" autocomplete="off" spellcheck="false" />
          <button class="btn btn-blue btn-sm" id="search-btn" onclick="searchUser()">Search</button>
        </div>
        <div id="search-result-wrap" class="result-wrap"></div>
        <div class="limit-bar"><div class="limit-fill" id="limit-fill" style="width:0%"></div></div>
      </div>
      <div id="managed-user-list">
        <div class="empty-managed">No users added yet — search a username above.</div>
      </div>
    </div>

    <!-- Monitor Settings -->
    <div class="settings-grid">

      <div class="settings-card">
        <h3>⚙️ Monitor Settings</h3>
        <div class="form-row">
          <label class="form-label">Check Interval (seconds)</label>
          <input type="number" id="cfg-interval" min="10" max="300" value="45">
        </div>
        <div class="form-row">
          <label class="form-label">Worker Count</label>
          <input type="number" id="cfg-workers" min="1" max="10" value="3">
        </div>
        <div class="toggle-row">
          <label>Headless Browser</label>
          <label class="toggle">
            <input type="checkbox" id="cfg-headless" checked>
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="toggle-row">
          <label>Notification Sound</label>
          <label class="toggle">
            <input type="checkbox" id="cfg-sound" checked>
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>

      <div class="settings-card">
        <h3>📊 Capacity</h3>
        <p style="font-size:13px;color:var(--gray-light);line-height:2">
          Max users &nbsp;<strong style="color:var(--white)">30</strong><br>
          Workers &nbsp;<strong style="color:var(--white)" id="cap-workers">3</strong><br>
          Per worker &nbsp;<strong style="color:var(--white)">10</strong><br>
          Cycle time &nbsp;<strong style="color:var(--white)">~45 s</strong>
        </p>
        <p style="font-size:11px;color:var(--gray);margin-top:12px">
          Added users are picked up on the next monitoring cycle (~45 s).
          Worker / headless changes need an app restart.
        </p>
      </div>

      <div class="settings-card settings-full">
        <div class="settings-save-row">
          <button class="btn btn-blue" onclick="saveSettings()">💾 Save Monitor Settings</button>
          <span class="save-msg" id="save-msg">✓ Saved!</span>
        </div>
      </div>

    </div>
  </div><!-- /settings -->

</div><!-- /main -->
</div><!-- /app -->

<!-- Toast container -->
<div id="toast-wrap"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let lastPostCount  = 0;
let lastDetections = [];
let currentPage    = 'dashboard';

// ── Navigation ─────────────────────────────────────────────────────────────
function nav(page) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));

  const items = document.querySelectorAll('.nav-item');
  const pages = ['dashboard','users','settings'];
  items[pages.indexOf(page)]?.classList.add('active');
  document.getElementById('page-' + page)?.classList.add('active');

  const titles = { dashboard: 'Dashboard', users: 'Users', settings: 'Settings' };
  document.getElementById('page-title').textContent = titles[page] || page;
  currentPage = page;

  if (page === 'settings') { loadManagedUsers(); refreshAuthBadge(); }
}

// ── Stop monitoring ────────────────────────────────────────────────────────
async function stopMonitor() {
  if (!confirm('Stop monitoring?')) return;
  await fetch('/api/control', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'stop'}) });
}

// ── Save settings ──────────────────────────────────────────────────────────
async function saveSettings() {
  const body = {
    interval: parseInt(document.getElementById('cfg-interval').value) || 45,
    workers:  parseInt(document.getElementById('cfg-workers').value)  || 3,
    headless: document.getElementById('cfg-headless').checked,
    sound:    document.getElementById('cfg-sound').checked,
  };
  await fetch('/api/settings', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body) });
  const msg = document.getElementById('save-msg');
  msg.classList.add('show');
  setTimeout(() => msg.classList.remove('show'), 2500);
}

// ── Toast ──────────────────────────────────────────────────────────────────
function showToast(det) {
  const wrap = document.getElementById('toast-wrap');
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML = `
    <div class="toast-icon">🔔</div>
    <div>
      <div class="toast-title">${det.display_name} <span style="color:var(--gray);font-weight:400">@${det.username}</span></div>
      <div class="toast-body">New post detected</div>
      <div class="toast-url" onclick="window.open('${det.post_url}','_blank')">${det.post_url}</div>
    </div>`;
  wrap.prepend(t);
  setTimeout(() => t.remove(), 7000);
}

// ── Main poll ──────────────────────────────────────────────────────────────
async function poll() {
  let data;
  try { data = await (await fetch('/api/status')).json(); }
  catch { return; }

  // Header / sidebar
  const live = data.is_monitoring;
  const dot  = document.getElementById('sb-dot');
  const stxt = document.getElementById('sb-status');
  dot.className  = 'status-dot ' + (live ? 'live pulse' : 'down');
  stxt.textContent = live ? 'LIVE' : 'STOPPED';
  stxt.style.color = live ? 'var(--green)' : 'var(--red)';
  document.getElementById('hdr-uptime').textContent = data.uptime || '--';
  document.getElementById('btn-stop').disabled = !live;

  // Stats
  document.getElementById('s-checks').textContent  = data.checked_count;
  document.getElementById('s-posts').textContent   = data.new_posts_count;
  document.getElementById('s-errors').textContent  = data.error_count;
  document.getElementById('s-workers').textContent = data.worker_count;

  // Toasts for new posts
  if (data.new_posts_count > lastPostCount && lastPostCount > 0) {
    const newOnes = data.detections.slice(0, data.new_posts_count - lastPostCount);
    newOnes.forEach(showToast);
  }
  lastPostCount = data.new_posts_count;

  // ── Detection feed ────────────────────────────────────────────────────
  const feed   = document.getElementById('det-feed');
  const dets   = data.detections;
  const badge  = document.getElementById('det-badge');
  badge.textContent = dets.length;

  if (dets.length === 0) {
    feed.innerHTML = '<div class="empty-feed"><div class="e-icon">👀</div>Monitoring… new posts will appear here.</div>';
  } else {
    const isNew = JSON.stringify(dets.map(d=>d.ts)) !== JSON.stringify(lastDetections);
    if (isNew) {
      feed.innerHTML = dets.map((d,i) => `
        <div class="det-row ${i===0?'fresh':''}">
          <div class="det-bell">🔔</div>
          <div class="det-body">
            <div class="det-name">${d.display_name} <span class="det-handle">@${d.username}</span></div>
            <a class="det-url" href="${d.post_url}" target="_blank" rel="noopener">${d.post_url}</a>
          </div>
          <div class="det-time">${d.time_ago}</div>
        </div>`).join('');
      lastDetections = dets.map(d=>d.ts);
    } else {
      // Just refresh time labels
      feed.querySelectorAll('.det-time').forEach((el,i) => {
        if (dets[i]) el.textContent = dets[i].time_ago;
      });
    }
  }

  // ── Mini user grid (dashboard) ────────────────────────────────────────
  const grid = document.getElementById('mini-user-grid');
  document.getElementById('usr-badge').textContent = data.users.length;
  grid.innerHTML = data.users.map(u => {
    const cls = u.ok === null ? 'pending' : u.ok ? 'ok' : 'err';
    return `
      <div class="user-card">
        <div class="u-dot ${cls}"></div>
        <div class="u-info">
          <div class="u-handle">@${u.username}</div>
          <div class="u-time">${u.last_checked}</div>
        </div>
      </div>`;
  }).join('');

  // ── Users page table ──────────────────────────────────────────────────
  document.getElementById('users-count-badge').textContent = data.users.length;
  document.getElementById('users-tbody').innerHTML = data.users.map(u => {
    const cls     = u.ok === null ? 'pending' : u.ok ? 'ok' : 'err';
    const lbl     = u.ok === null ? 'Pending' : u.ok ? 'OK' : 'Error';
    const dot     = u.ok === null ? '○' : '●';
    const href    = u.post_id ? `https://x.com/${u.username}/status/${u.post_id}` : null;
    const dname   = u.display_name || '';
    const initial = (dname || u.username)[0].toUpperCase();
    return `
      <tr>
        <td>
          <div style="display:flex;align-items:center;gap:10px">
            ${avatarHtml(u.username, initial, 32)}
            <div>
              <div style="font-weight:700">${dname ? esc(dname) : ''}</div>
              <div style="color:var(--gray);font-size:12px">@${u.username}</div>
            </div>
          </div>
        </td>
        <td><span class="badge ${cls}">${dot} ${lbl}</span></td>
        <td style="color:var(--gray)">${u.last_checked}</td>
        <td>${href ? `<a class="post-link" href="${href}" target="_blank" rel="noopener">${href}</a>` : '<span style="color:var(--gray)">—</span>'}</td>
      </tr>`;
  }).join('');
}

// ── Load settings into form ────────────────────────────────────────────────
async function loadSettings() {
  try {
    const s = await (await fetch('/api/settings')).json();
    document.getElementById('cfg-interval').value   = s.interval;
    document.getElementById('cfg-workers').value    = s.workers;
    document.getElementById('cfg-headless').checked = s.headless;
    document.getElementById('cfg-sound').checked    = s.sound;
  } catch {}
}

// ── Manage Users ───────────────────────────────────────────────────────────
let _managedUsers = [];
let _st = null;  // debounce timer

function onSearchType() {
  clearTimeout(_st);
  const raw = document.getElementById('search-input').value.trim().replace(/^@+/, '');
  if (raw.length < 2) {
    document.getElementById('search-result-wrap').innerHTML = '';
    return;
  }
  _st = setTimeout(searchUser, 800);
}

async function searchUser() {
  clearTimeout(_st);
  const input = document.getElementById('search-input');
  const btn   = document.getElementById('search-btn');
  const raw   = input.value.trim().replace(/^@+/, '');
  if (!raw) { input.focus(); return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  document.getElementById('search-result-wrap').innerHTML = '';

  try {
    const res  = await fetch('/api/users/search', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: raw}),
    });
    const data = await res.json();
    if (data.multiple) renderMultiResults(data.results, raw);
    else               renderSearchResult(data);
  } catch {
    renderSearchResult({found: false, reason: 'Network error'});
  } finally {
    btn.disabled  = false;
    btn.textContent = 'Search';
  }
}

function avatarHtml(username, fallbackInitial, size) {
  const s = size || 44;
  return '<img src="https://unavatar.io/twitter/' + escAttr(username) + '" '
    + 'width="' + s + '" height="' + s + '" '
    + 'style="border-radius:50%;object-fit:cover;flex-shrink:0;display:block;" '
    + 'onerror="this.outerHTML=\'<div style=&quot;width:' + s + 'px;height:' + s + 'px;border-radius:50%;'
    + 'background:var(--blue);display:grid;place-items:center;font-size:' + Math.round(s*0.4) + 'px;'
    + 'font-weight:800;color:#fff;flex-shrink:0&quot;>' + escAttr(fallbackInitial) + '</div>\'" />';
}

function renderSearchResult(data) {
  const wrap = document.getElementById('search-result-wrap');
  if (!data.found) {
    wrap.innerHTML =
      '<div class="result-error">❌ &nbsp;' + esc(data.reason || 'User not found') + '</div>';
    return;
  }
  const name    = data.display_name || data.username;
  const initial = name[0].toUpperCase();
  const already = _managedUsers.some(u => u.username.toLowerCase() === data.username.toLowerCase());
  const atLimit = _managedUsers.length >= 30;

  const postHtml = data.latest_post_url
    ? '<a class="det-url" href="' + escAttr(data.latest_post_url) + '" target="_blank" rel="noopener"'
      + ' style="display:block;margin-top:5px;font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
      + '🔗 Latest: ' + esc(data.latest_post_url) + '</a>'
    : '';

  let action = '';
  if (already)       action = '<span class="already-tag">✓ Already added</span>';
  else if (atLimit)  action = '<span class="limit-tag">Limit reached (30/30)</span>';
  else               action =
    '<button class="btn btn-blue btn-sm" '
    + 'data-uname="'    + escAttr(data.username)                       + '" '
    + 'data-dname="'    + escAttr(name)                                 + '" '
    + (data.latest_post_id  ? 'data-postid="'  + escAttr(data.latest_post_id)  + '" ' : '')
    + (data.latest_post_url ? 'data-posturl="' + escAttr(data.latest_post_url) + '" ' : '')
    + 'onclick="addUserFromBtn(this)">+ Add</button>';

  wrap.innerHTML =
    '<div class="result-card">'
    + avatarHtml(data.username, initial, 44)
    + '<div class="result-info">'
    + '<div class="result-name">'    + esc(name)           + '</div>'
    + '<div class="result-handle">@' + esc(data.username)  + '</div>'
    + postHtml
    + '</div>' + action + '</div>';
}

function renderMultiResults(results, query) {
  const wrap = document.getElementById('search-result-wrap');
  if (!results || !results.length) {
    wrap.innerHTML = '<div class="result-error">❌ &nbsp;No users found for "' + esc(query) + '"</div>';
    return;
  }
  wrap.innerHTML =
    '<div style="font-size:12px;color:var(--gray);margin-bottom:10px">'
    + results.length + ' result(s) for "' + esc(query) + '"</div>'
    + '<div class="multi-results">'
    + results.map(u => {
        const name    = u.display_name || u.username;
        const initial = name[0].toUpperCase();
        const already = _managedUsers.some(m => m.username.toLowerCase() === u.username.toLowerCase());
        return '<div class="multi-result-card">'
          + avatarHtml(u.username, initial, 40)
          + '<div class="result-info">'
          + '<div class="result-name">'    + esc(name)         + '</div>'
          + '<div class="result-handle">@' + esc(u.username)   + '</div>'
          + '</div>'
          + (already
              ? '<span class="already-tag">✓ Added</span>'
              : '<button class="btn btn-blue btn-sm" '
                + 'data-uname="' + escAttr(u.username) + '" '
                + 'data-dname="' + escAttr(name) + '" '
                + 'onclick="addUserFromBtn(this)">+ Add</button>')
          + '</div>';
      }).join('')
    + '</div>';
}

function addUserFromBtn(btn) {
  addUser(btn.dataset.uname, btn.dataset.dname, btn.dataset.postid || null, btn.dataset.posturl || null);
}

async function addUser(username, displayName, initialPostId, initialPostUrl) {
  const res  = await fetch('/api/users/add', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      username,
      display_name:     displayName,
      initial_post_id:  initialPostId  || null,
      initial_post_url: initialPostUrl || null,
    }),
  });
  const data = await res.json();
  if (data.ok) {
    document.getElementById('search-result-wrap').innerHTML = '';
    document.getElementById('search-input').value = '';
    loadManagedUsers();
  } else {
    document.getElementById('search-result-wrap').innerHTML =
      '<div class="result-error">❌ &nbsp;' + esc(data.reason || 'Could not add user') + '</div>';
  }
}

async function removeUser(username) {
  if (!confirm('Remove @' + username + ' from monitoring?')) return;
  await fetch('/api/users/' + encodeURIComponent(username), {method: 'DELETE'});
  loadManagedUsers();
}

async function loadManagedUsers() {
  try {
    const data  = await (await fetch('/api/status')).json();
    const users = data.users || [];
    _managedUsers = users;

    const count   = users.length;
    const maxU    = data.max_users || 30;
    const pill    = document.getElementById('count-pill');
    if (pill) {
      pill.textContent = count + ' / ' + maxU;
      pill.className   = 'count-pill ' + (count >= maxU ? 'full' : count >= maxU * .83 ? 'warn' : 'ok');
    }
    const fill = document.getElementById('limit-fill');
    if (fill) {
      const pct = Math.min(100, (count / maxU) * 100);
      fill.style.width = pct + '%';
      fill.className   = 'limit-fill' + (pct >= 100 ? ' full' : pct >= 83 ? ' warn' : '');
    }
    const cw = document.getElementById('cap-workers');
    if (cw) cw.textContent = data.worker_count || 3;

    renderManagedUsers(users);
  } catch {}
}

function renderManagedUsers(users) {
  const list = document.getElementById('managed-user-list');
  if (!list) return;
  if (!users.length) {
    list.innerHTML = '<div class="empty-managed">No users added yet — search a username above.</div>';
    return;
  }
  list.innerHTML = users.map(u => {
    const name     = u.display_name || ('@' + u.username);
    const initial  = (u.display_name || u.username)[0].toUpperCase();
    const dotColor = u.ok === null ? 'var(--gray)' : u.ok ? 'var(--green)' : 'var(--red)';
    return '<div class="managed-row">'
      + avatarHtml(u.username, initial, 36)
      + '<div class="managed-info">'
      + '<div class="managed-dname">'  + esc(name) + '</div>'
      + '<div class="managed-meta">@'  + esc(u.username)
      +   ' &nbsp;<span style="color:' + dotColor + '">&#9679;</span>'
      +   ' <span>' + esc(u.last_checked || 'Pending') + '</span>'
      + '</div></div>'
      + '<button class="btn-remove" data-u="' + escAttr(u.username) + '" '
      + 'onclick="removeUser(this.dataset.u)" title="Remove">✕</button>'
      + '</div>';
  }).join('');
}

function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s) {
  return String(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Twitter Auth ───────────────────────────────────────────────────────────
let _authPollTimer  = null;
let _loginWasPending = false;   // track if we were waiting for login to complete

async function loadAuthStatus() {
  try {
    const d = await (await fetch('/api/auth/status')).json();
    renderAuthCard(d);
  } catch {}
}

function showLoginSuccessModal(handle) {
  // Remove any existing modal
  document.getElementById('login-success-modal')?.remove();

  const modal = document.createElement('div');
  modal.id = 'login-success-modal';
  modal.style.cssText =
    'position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.75);'
    + 'display:flex;align-items:center;justify-content:center;animation:fadein .3s ease';
  modal.innerHTML =
    '<div style="background:#16181c;border:2px solid var(--green);border-radius:20px;'
    + 'padding:44px 40px;text-align:center;max-width:420px;width:90%;box-shadow:0 20px 60px rgba(0,0,0,.6)">'
    + '<div style="font-size:64px;margin-bottom:16px">✅</div>'
    + '<div style="font-size:26px;font-weight:900;color:var(--green);margin-bottom:8px">Connected!</div>'
    + '<div style="font-size:15px;color:var(--gray-light);margin-bottom:20px">'
    + (handle && handle !== 'Connected Account' ? '<b style="color:var(--white)">' + esc(handle) + '</b> ka Twitter account connect ho gaya.' : 'Twitter account successfully connect ho gaya.')
    + '</div>'
    + '<div style="display:flex;gap:12px;justify-content:center">'
    + '<button class="btn btn-blue" id="modal-go-btn" onclick="document.getElementById(\'login-success-modal\').remove();nav(\'dashboard\')">'
    + '📊 Dashboard Dekho</button>'
    + '</div>'
    + '</div>';
  document.body.appendChild(modal);

  // Close on backdrop click
  modal.addEventListener('click', function(e) {
    if (e.target === modal) { modal.remove(); nav('dashboard'); }
  });

  // Auto-redirect to dashboard after 2.5s
  setTimeout(function() {
    modal.remove();
    nav('dashboard');
    loadAuthStatus();
  }, 2500);
}

async function doQuickLogin() {
  const banner = document.getElementById('login-banner');
  const btn = banner?.querySelector('button');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Connecting…'; }

  // Try silent Edge/Chrome import first
  try {
    const res = await fetch('/api/auth/import-browser', {method:'POST'});
    const d   = await res.json();
    if (d.ok) {
      showLoginSuccessModal(null);
      loadAuthStatus();
      return;
    }
  } catch {}

  // Fallback: open browser window login
  if (btn) { btn.disabled = false; btn.textContent = '🔑 Twitter Login Karo'; }
  startLogin();
}

function renderAuthCard(d) {
  // Show/hide dashboard login banner
  const banner = document.getElementById('login-banner');
  if (banner) banner.style.display = d.logged_in ? 'none' : 'flex';

  const badge = document.getElementById('auth-status-badge');
  const body  = document.getElementById('auth-body');
  if (!badge || !body) return;

  if (d.login_in_progress) {
    _loginWasPending = true;
    badge.textContent = 'Waiting…';
    badge.style.background = 'rgba(255,112,67,.15)';
    badge.style.color = 'var(--orange)';
    body.innerHTML =
      '<div style="background:rgba(255,112,67,.08);border:1px solid rgba(255,112,67,.25);border-radius:10px;padding:16px 18px">'
      + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">'
      + '<span class="spinner" style="border-top-color:var(--orange);flex-shrink:0"></span>'
      + '<span style="font-size:14px;font-weight:700;color:var(--white)">Tumhare browser mein login ka wait kar raha hoon…</span>'
      + '</div>'
      + '<div style="font-size:13px;color:var(--gray-light);line-height:1.6;margin-bottom:14px">'
      + '👉 <b style="color:var(--white)">x.com</b> ka tab already khul gaya hoga — wahan <b style="color:var(--green)">Sign in with Google</b> click karo.<br>'
      + 'Login hote hi yeh page auto update ho jaayega.'
      + '</div>'
      + '<button class="btn btn-ghost btn-sm" onclick="cancelLogin()" style="font-size:12px">✕ Cancel</button>'
      + '</div>';
    if (!_authPollTimer)
      _authPollTimer = setInterval(loadAuthStatus, 2000);
    return;
  }

  // Login just completed — close guide overlay and show success modal
  if (_loginWasPending && d.login_done && d.login_result && d.login_result.ok) {
    _loginWasPending = false;
    clearInterval(_authPollTimer);
    _authPollTimer = null;
    document.getElementById('login-guide-overlay')?.remove();
    const uname  = d.username || '';
    const handle = (!uname || uname === 'unknown') ? null : (uname.startsWith('@') ? uname : '@' + uname);
    showLoginSuccessModal(handle);
  } else {
    clearInterval(_authPollTimer);
    _authPollTimer = null;
  }

  if (d.logged_in) {
    const uname = d.username || '';
    const isUnknown = !uname || uname === 'unknown';
    const handle = isUnknown ? 'Connected Account' : (uname.startsWith('@') ? uname : ('@' + uname));
    const bare   = isUnknown ? '' : handle.replace('@', '');
    badge.textContent = '✓ Connected';
    badge.style.background = 'rgba(0,186,124,.15)';
    badge.style.color = 'var(--green)';
    const avatarHtmlStr = bare
      ? '<img class="auth-avatar" src="https://unavatar.io/twitter/' + escAttr(bare) + '" onerror="this.style.display=\'none\'" />'
      : '<div style="width:46px;height:46px;border-radius:50%;background:var(--blue);display:grid;place-items:center;font-size:22px;flex-shrink:0">𝕏</div>';
    body.innerHTML =
      '<div class="auth-connected">'
      + avatarHtmlStr
      + '<div class="auth-info">'
      + '<div class="auth-name">' + esc(handle) + '</div>'
      + '<div class="auth-sub">Session active &nbsp;✓&nbsp; Search by real name enabled &nbsp;✓</div>'
      + '</div>'
      + '<button class="btn btn-ghost btn-sm" onclick="disconnectTwitter()">Disconnect</button>'
      + '</div>';
  } else {
    badge.textContent = 'Not connected';
    badge.style.background = 'rgba(113,118,123,.12)';
    badge.style.color = 'var(--gray)';
    body.innerHTML =
      '<div style="display:flex;flex-direction:column;gap:14px">'

      // Option 1 — Import from browser (PRIMARY, no popup)
      + '<div style="background:rgba(29,155,240,.07);border:1px solid rgba(29,155,240,.2);border-radius:10px;padding:14px 16px">'
      + '<div style="font-size:13px;font-weight:700;color:var(--white);margin-bottom:4px">⚡ Instant — Edge/Chrome se Import karo</div>'
      + '<div style="font-size:12px;color:var(--gray-light);margin-bottom:10px">Agar Edge ya Chrome mein x.com pe already login ho — koi popup nahi, koi window nahi, seedha import.</div>'
      + '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
      + '<button class="btn btn-blue btn-sm" id="import-btn" onclick="importBrowserSession()">⬇ Browser se Import Karo</button>'
      + '</div>'
      + '<div id="import-msg" style="margin-top:8px;font-size:12px;color:var(--gray)"></div>'
      + '</div>'

      // Option 2 — Open browser window (SECONDARY)
      + '<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px">'
      + '<div style="font-size:13px;font-weight:700;color:var(--gray-light);margin-bottom:4px">🌐 Google se Login karo</div>'
      + '<div style="font-size:12px;color:var(--gray);margin-bottom:10px">Tumhara apna Edge/Chrome browser khulega — wahan <b style="color:var(--white)">Sign in with Google</b> click karo. Password nahi daalna.</div>'
      + '<button class="btn btn-ghost btn-sm" id="login-btn" onclick="startLogin()">🔑 Google se Login Karo</button>'
      + '<div id="login-msg" style="margin-top:8px;font-size:12px;color:var(--gray)"></div>'
      + '</div>'

      // Option 3 — Manual cookie paste (most reliable, no dependencies)
      + '<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px">'
      + '<div style="font-size:13px;font-weight:700;color:var(--gray-light);margin-bottom:4px">🍪 Manual Cookie — Sabse Reliable</div>'
      + '<div style="font-size:12px;color:var(--gray);margin-bottom:8px">Browser DevTools se directly cookies paste karo — koi popup nahi, koi install nahi.</div>'
      + '<span class="cookie-toggle" onclick="toggleManualForm()">▶ Step-by-step guide dikhao</span>'
      + '<div id="manual-form" style="display:none;margin-top:12px">'
      + '<div class="cookie-steps">'
      + '1. Browser mein <b>x.com</b> kholo aur login karo<br>'
      + '2. <b>F12</b> dabao → <b>Application</b> tab → <b>Cookies</b> → <b>https://x.com</b><br>'
      + '3. Neeche <b>auth_token</b> dhundho → value copy karo<br>'
      + '4. Phir <b>ct0</b> dhundho → value copy karo<br>'
      + '5. Dono yahan paste karo ↓'
      + '</div>'
      + '<div class="cookie-fields">'
      + '<input type="password" id="manual-auth-token" placeholder="auth_token value yahan paste karo" autocomplete="off" />'
      + '<input type="text"     id="manual-ct0"        placeholder="ct0 value yahan paste karo"        autocomplete="off" />'
      + '</div>'
      + '<button class="btn btn-blue btn-sm" id="manual-btn" onclick="manualLogin()">✅ Connect Karo</button>'
      + '<div id="manual-msg" style="margin-top:8px;font-size:12px;color:var(--gray)"></div>'
      + '</div>'
      + '</div>'

      + '</div>';
  }
}

async function importBrowserSession() {
  const btn = document.getElementById('import-btn');
  const msg = document.getElementById('import-msg');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Connecting…'; }
  if (msg) { msg.style.color = 'var(--gray)'; msg.textContent = 'Edge browser se cookies import ho rahi hain…'; }
  try {
    const res = await fetch('/api/auth/import-browser', {method:'POST'});
    const d   = await res.json();
    if (d.ok) {
      document.getElementById('login-guide-overlay')?.remove();
      showLoginSuccessModal(null);
      loadAuthStatus();
    } else {
      const reason = d.reason || 'Failed';
      // Show helpful hint if Edge login missing
      const hint = reason.toLowerCase().includes('nahi mila') || reason.toLowerCase().includes('not found')
        ? ' — pehle Edge browser mein x.com pe login karein'
        : '';
      if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + reason + hint; }
      if (btn) { btn.disabled = false; btn.innerHTML = '2. Connect ✓'; }
    }
  } catch {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Network error — app chal raha hai?'; }
    if (btn) { btn.disabled = false; btn.innerHTML = '2. Connect ✓'; }
  }
}

async function startLogin() {
  const btn = document.getElementById('login-btn');
  const msg = document.getElementById('login-msg');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Browser khul raha hai…'; }
  if (msg) { msg.style.color = 'var(--gray)'; msg.textContent = 'Apne browser mein Twitter login karo (Google se bhi ho sakta hai) — yeh page auto update ho jaayega'; }
  try {
    const res = await fetch('/api/auth/login', {method:'POST'});
    const d   = await res.json();
    if (d.started) {
      _loginWasPending = true;
      loadAuthStatus();
    } else {
      if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + (d.message || 'Start nahi hua'); }
      if (btn) { btn.disabled = false; btn.innerHTML = 'Browser Window Kholo'; }
    }
  } catch {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Network error'; }
    if (btn) { btn.disabled = false; btn.innerHTML = 'Browser Window Kholo'; }
  }
}

async function connectTwitter() {
  return startLogin();
}

function toggleManualForm() {
  const form   = document.getElementById('manual-form');
  const toggle = form?.previousElementSibling;
  if (!form) return;
  const open = form.style.display === 'none';
  form.style.display = open ? 'block' : 'none';
  if (toggle) toggle.textContent = open ? '▼ Guide band karo' : '▶ Step-by-step guide dikhao';
}

async function manualLogin() {
  const authToken = (document.getElementById('manual-auth-token')?.value || '').trim();
  const ct0       = (document.getElementById('manual-ct0')?.value || '').trim();
  const btn       = document.getElementById('manual-btn');
  const msg       = document.getElementById('manual-msg');

  if (!authToken || !ct0) {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Dono fields fill karo'; }
    return;
  }
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Connecting…'; }
  if (msg) { msg.style.color = 'var(--gray)'; msg.textContent = 'Cookies verify ho rahi hain…'; }

  try {
    const res = await fetch('/api/auth/manual', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({auth_token: authToken, ct0}),
    });
    const d = await res.json();
    if (d.ok) {
      document.getElementById('manual-auth-token').value = '';
      document.getElementById('manual-ct0').value = '';
      showLoginSuccessModal(d.username && d.username !== 'unknown' ? '@' + d.username.replace('@','') : null);
      loadAuthStatus();
    } else {
      if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + (d.reason || 'Failed'); }
      if (btn) { btn.disabled = false; btn.textContent = '✅ Connect Karo'; }
    }
  } catch {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Network error'; }
    if (btn) { btn.disabled = false; btn.textContent = '✅ Connect Karo'; }
  }
}

async function cancelLogin() {
  await fetch('/api/auth/cancel', {method:'POST'});
  _loginWasPending = false;
  clearInterval(_authPollTimer);
  _authPollTimer = null;
  loadAuthStatus();
}

async function disconnectTwitter() {
  if (!confirm('Twitter account disconnect karein? Monitoring bina login ke chalti rahegi.')) return;
  _loginWasPending = false;
  await fetch('/api/auth/logout', {method:'POST'});
  loadAuthStatus();
}

// ── Twitter Auth ───────────────────────────────────────────────────────────
async function refreshAuthBadge() {
  try {
    const d = await (await fetch('/api/auth/status')).json();
    const badge     = document.getElementById('auth-badge');
    const form      = document.getElementById('auth-form');
    const connected = document.getElementById('auth-connected');
    const info      = document.getElementById('auth-info');
    if (d.logged_in) {
      const handle = d.username && d.username !== 'unknown' ? '@' + d.username.replace('@','') : 'Connected';
      if (badge)     { badge.textContent='✓ '+handle; badge.style.background='rgba(0,186,124,.15)'; badge.style.color='var(--green)'; }
      if (info)      info.textContent = '✓ ' + handle + ' — fast monitoring active';
      if (form)      form.style.display = 'none';
      if (connected) connected.style.display = 'flex';
    } else {
      if (badge)     { badge.textContent='Not connected'; badge.style.background='rgba(113,118,123,.15)'; badge.style.color='var(--gray)'; }
      if (form)      form.style.display = 'block';
      if (connected) connected.style.display = 'none';
    }
  } catch {}
}

async function doManualConnect() {
  const authToken = (document.getElementById('mc-auth')?.value || '').trim();
  const ct0       = (document.getElementById('mc-ct0')?.value  || '').trim();
  const btn = document.getElementById('mc-btn');
  const msg = document.getElementById('mc-msg');
  if (!authToken || !ct0) { if(msg){msg.style.color='var(--red)';msg.textContent='❌ Dono fields fill karo';} return; }
  if (btn) { btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Connecting…'; }
  if (msg) { msg.style.color='var(--gray)'; msg.textContent='Verify ho raha hai…'; }
  try {
    const res = await fetch('/api/auth/manual', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({auth_token: authToken, ct0}),
    });
    const d = await res.json();
    if (d.ok) {
      document.getElementById('mc-auth').value = '';
      document.getElementById('mc-ct0').value  = '';
      showLoginSuccessModal(d.username && d.username !== 'unknown' ? '@'+d.username.replace('@','') : null);
      refreshAuthBadge();
    } else {
      if (msg){msg.style.color='var(--red)'; msg.textContent='❌ '+(d.reason||'Failed');}
      if (btn){btn.disabled=false; btn.textContent='✅ Connect Karo';}
    }
  } catch {
    if (msg){msg.style.color='var(--red)'; msg.textContent='❌ Network error';}
    if (btn){btn.disabled=false; btn.textContent='✅ Connect Karo';}
  }
}

async function doLogout() {
  if (!confirm('Twitter disconnect karein?')) return;
  await fetch('/api/auth/logout', {method:'POST'});
  refreshAuthBadge();
}

// ── Init ──────────────────────────────────────────────────────────────────
poll();
loadSettings();
loadManagedUsers();
refreshAuthBadge();
setInterval(poll, 3000);
</script>
</body>
</html>"""
