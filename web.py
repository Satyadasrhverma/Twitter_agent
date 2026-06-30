"""
Web dashboard server — http://localhost:8080
FastAPI backend + single-page HTML/CSS/JS frontend.
Multi-user: each request is authenticated via an x_session cookie.
"""

import asyncio
import json as _json
import re as _re
import sqlite3
import threading
import urllib.parse as _urlparse
import urllib.request as _urllib
import webbrowser
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import app_auth
import auth
import config
import whatsapp
from ui_state import AppState

app_auth.init_tables()   # ensure app_users table exists

# ── Globals set by start_server() ──────────────────────────────────────────
_user_manager: Optional[Any] = None   # gui.UserMonitorManager | None

app = FastAPI(title="X Monitor", docs_url=None, redoc_url=None)
PORT = 8080


# ── Session dependency ──────────────────────────────────────────────────────

async def _current_user(x_session: str = Cookie(None)) -> Optional[dict]:
    """Return {user_id, username} for a valid session cookie, else None."""
    if not x_session:
        return None
    return app_auth.get_session(x_session)



def _get_state(user_id: int) -> AppState:
    return _user_manager.get_state(user_id)  # type: ignore[union-attr]


def _get_monitor(user_id: int) -> Optional[Any]:
    return _user_manager.get_monitor(user_id)  # type: ignore[union-attr]


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(user: Optional[dict] = Depends(_current_user)) -> str:
    if not user:
        return _LOGIN_HTML
    return _HTML


@app.post("/app/logout")
async def app_logout(response: Response, x_session: str = Cookie(None)) -> JSONResponse:
    if x_session:
        app_auth.logout(x_session)
    response.delete_cookie("x_session")
    return JSONResponse({"ok": True})


# ── Google OAuth ──────────────────────────────────────────────────────────────

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_INFO_URL  = "https://www.googleapis.com/oauth2/v2/userinfo"


def _google_redirect_uri() -> str:
    return f"http://localhost:{PORT}/auth/google/callback"


@app.get("/auth/google")
async def google_login() -> RedirectResponse:
    if not config.GOOGLE_CLIENT_ID:
        return RedirectResponse("/?error=setup")
    params = _urlparse.urlencode({
        "client_id":     config.GOOGLE_CLIENT_ID,
        "redirect_uri":  _google_redirect_uri(),
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "prompt":        "select_account",
    })
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{params}")


@app.get("/auth/google/callback")
async def google_callback(
    code:  Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    import logging as _log2
    _logger = _log2.getLogger(__name__)

    if error or not code:
        _logger.error("Google callback: error=%s code_present=%s", error, bool(code))
        return RedirectResponse("/?error=cancelled")
    try:
        _logger.info("Google callback: exchanging code for token...")
        token_data = _urlparse.urlencode({
            "code":          code,
            "client_id":     config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri":  _google_redirect_uri(),
            "grant_type":    "authorization_code",
        }).encode()
        req = _urllib.Request(_GOOGLE_TOKEN_URL, data=token_data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with _urllib.urlopen(req, timeout=15) as r:
            token_resp = _json.loads(r.read())

        _logger.info("Token response keys: %s", list(token_resp.keys()))
        access_token = token_resp.get("access_token", "")
        if not access_token:
            _logger.error("No access_token in response: %s", token_resp)
            return RedirectResponse("/?error=failed")

        _logger.info("Got access_token, fetching user info...")
        info_req = _urllib.Request(
            f"{_GOOGLE_INFO_URL}?access_token={_urlparse.quote(access_token)}"
        )
        with _urllib.urlopen(info_req, timeout=15) as r:
            user_info = _json.loads(r.read())

        google_id = user_info.get("id", "")
        email     = user_info.get("email", "")
        name      = user_info.get("name", "")
        _logger.info("Google user: email=%s name=%s id_present=%s", email, name, bool(google_id))

        if not google_id:
            return RedirectResponse("/?error=failed")

        token, user_id = await asyncio.to_thread(
            app_auth.get_or_create_google_user, google_id, email, name
        )
        _logger.info("User saved: user_id=%d email=%s", user_id, email)
        try:
            if _user_manager:
                _user_manager.ensure_running(user_id)
        except Exception as e:
            _logger.warning("Monitor start failed (non-fatal): %s", e)

        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("x_session", token, httponly=True, samesite="lax", max_age=86400 * 30)
        return resp

    except Exception as exc:
        _logger.error("Google OAuth error: %s", exc, exc_info=True)
        return RedirectResponse("/?error=failed")


@app.get("/api/me")
async def api_me(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return JSONResponse({"user_id": user["user_id"], "username": user["username"], "name": user.get("name", "")})


@app.get("/api/status")
async def get_status(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    uid = user["user_id"]
    s   = _get_state(uid)
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
async def get_settings(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return JSONResponse({
        "interval":  config.CHECK_INTERVAL_SECONDS,
        "workers":   config.WORKER_COUNT,
        "headless":  config.HEADLESS,
        "sound":     config.NOTIFICATION_SOUND,
        "max_users": config.MAX_MONITORED_USERS,
    })


class SettingsIn(BaseModel):
    interval: int
    workers:  int
    headless: bool
    sound:    bool


@app.post("/api/settings")
async def save_settings(body: SettingsIn, user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
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
            return None
        except Exception:
            return None
    return await asyncio.to_thread(_fetch)


@app.post("/api/users/search")
async def search_user_endpoint(body: UsernameIn, user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    uid     = user["user_id"]
    monitor = _get_monitor(uid)
    try:
        query = body.username.strip().lstrip("@")
        if not query:
            return JSONResponse({"found": False, "reason": "Empty username"})

        has_space = " " in query
        if auth.session_exists() and monitor is not None and has_space:
            try:
                results = await asyncio.to_thread(monitor.search_users_by_name, query)
                if results:
                    return JSONResponse({"found": True, "multiple": True, "results": results})
            except Exception:
                pass

        result: Optional[dict] = None
        try:
            result = await _quick_search(query)
        except Exception:
            pass

        if result is None:
            if monitor is None:
                return JSONResponse({"found": False, "reason": "Monitor not ready — try again in a moment"})
            try:
                result = await asyncio.to_thread(monitor.search_user, query)
            except Exception as exc:
                return JSONResponse({"found": False, "reason": f"Search failed: {exc}"})

        if not result:
            return JSONResponse({"found": False, "reason": "User not found"})

        if result.get("found") and monitor is not None:
            try:
                post = await asyncio.to_thread(monitor.get_latest_post, result["username"])
                if post:
                    result["latest_post_id"]  = post["post_id"]
                    result["latest_post_url"] = post["post_url"]
            except Exception:
                pass

        return JSONResponse(result)

    except Exception as exc:
        return JSONResponse({"found": False, "reason": str(exc)})


def _seed_tweet_sync(owner_id: int, username: str, post_id: str, post_url: str) -> None:
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=5)
        conn.execute(
            """INSERT INTO monitored_users (owner_id, username, latest_post_id, latest_post_url)
               VALUES (?, ?, ?, ?) ON CONFLICT(owner_id, username) DO NOTHING""",
            (owner_id, username.lower(), post_id, post_url),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("Could not seed tweet for @%s: %s", username, exc)


@app.post("/api/users/add")
async def add_user_endpoint(body: AddUserIn, user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    uid = user["user_id"]
    s   = _get_state(uid)
    ok, reason = s.add_user(body.username, body.display_name)
    if ok and body.initial_post_id:
        await asyncio.to_thread(
            _seed_tweet_sync,
            uid,
            body.username,
            body.initial_post_id,
            body.initial_post_url or "",
        )
    return JSONResponse({
        "ok":         ok,
        "reason":     reason,
        "user_count": len(s.get_monitored_users()),
        "max_users":  config.MAX_MONITORED_USERS,
    })


@app.delete("/api/users/{username}")
async def remove_user_endpoint(username: str, user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    s = _get_state(user["user_id"])
    removed = s.remove_user(username)
    return JSONResponse({"ok": removed, "user_count": len(s.get_monitored_users())})


# ── Twitter auth endpoints (app-level, shared — admin only) ──────────────────
# Twitter login is connected ONCE by the admin and reused by every app-user's
# monitoring. Other users never need to share their own Twitter credentials.

@app.get("/api/auth/status")
async def auth_status(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    uid      = user["user_id"]
    login_st = auth.get_login_state()
    return JSONResponse({
        "logged_in":         auth.session_exists(),
        "username":          auth.get_session_username(),
        "login_in_progress": login_st["in_progress"],
        "login_done":        login_st["done"],
        "login_result":      login_st["result"],
        "is_admin":          app_auth.is_admin(uid),
    })


@app.post("/api/auth/login")
async def auth_login(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    if not app_auth.is_admin(user["user_id"]):
        return JSONResponse({"started": False, "message": "Only the app admin can connect Twitter."})
    started, msg = auth.start_login()
    return JSONResponse({"started": started, "message": msg})


@app.post("/api/auth/logout")
async def auth_logout(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    if not app_auth.is_admin(user["user_id"]):
        return JSONResponse({"ok": False, "reason": "Only the app admin can disconnect Twitter."})
    auth.clear_session()
    return JSONResponse({"ok": True})


@app.post("/api/auth/cancel")
async def auth_cancel() -> JSONResponse:
    auth._set_state(in_progress=False, done=False, result=None)
    return JSONResponse({"ok": True})


class ManualCookieIn(BaseModel):
    auth_token: str
    ct0:        str


@app.post("/api/auth/manual")
async def auth_manual(body: ManualCookieIn, user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    if not app_auth.is_admin(user["user_id"]):
        return JSONResponse({"ok": False, "reason": "Only the app admin can connect Twitter."})
    ok, result = await asyncio.to_thread(auth.save_manual_cookies, body.auth_token, body.ct0)
    return JSONResponse({"ok": ok, "username": result} if ok else {"ok": False, "reason": result})


@app.post("/api/auth/import-browser")
async def auth_import_browser(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    if not app_auth.is_admin(user["user_id"]):
        return JSONResponse({"ok": False, "reason": "Only the app admin can connect Twitter."})
    ok, reason = await asyncio.to_thread(auth.import_from_browser)
    return JSONResponse({"ok": ok, "reason": reason})


# ── WhatsApp notification number (per-user) ──────────────────────────────────

class WhatsAppIn(BaseModel):
    number: str


@app.get("/api/whatsapp")
async def whatsapp_get(user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    number = await asyncio.to_thread(app_auth.get_whatsapp_number, user["user_id"])
    return JSONResponse({"number": number, "configured": whatsapp.is_configured()})


@app.post("/api/whatsapp")
async def whatsapp_set(body: WhatsAppIn, user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    number = body.number.strip()
    if number and not _re.match(r"^\+?[0-9]{8,15}$", number):
        return JSONResponse({"ok": False, "reason": "Enter a valid phone number with country code (e.g. +919876543210)"})
    await asyncio.to_thread(app_auth.set_whatsapp_number, user["user_id"], number)
    return JSONResponse({"ok": True})


class ControlIn(BaseModel):
    action: str


@app.post("/api/control")
async def control(body: ControlIn, user: Optional[dict] = Depends(_current_user)) -> JSONResponse:
    if not user:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    monitor = _get_monitor(user["user_id"])
    if body.action == "stop" and monitor is not None:
        monitor.stop()
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "reason": "unknown action"})


# ── Server lifecycle ────────────────────────────────────────────────────────

def start_server(user_manager: Any, open_browser: bool = True) -> threading.Thread:
    global _user_manager
    _user_manager = user_manager

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


# ── Login / Register page ────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>X Monitor</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#000;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#16181c;border:1px solid #2f3336;border-radius:20px;padding:48px 40px;width:100%;max-width:380px;box-shadow:0 24px 80px rgba(0,0,0,.7);text-align:center}
.logo{width:56px;height:56px;border-radius:50%;background:#1d9bf0;display:grid;place-items:center;font-size:28px;font-weight:900;color:#fff;margin:0 auto 20px}
h1{color:#e7e9ea;font-size:22px;font-weight:800;margin-bottom:6px}
.sub{color:#71767b;font-size:14px;margin-bottom:32px}
.btn-google{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;padding:14px 20px;border-radius:12px;background:#fff;color:#1f1f1f;font-size:15px;font-weight:600;border:none;cursor:pointer;transition:.15s;text-decoration:none}
.btn-google:hover{background:#f1f3f4;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.btn-google:active{background:#e8eaed}
.btn-google.loading{opacity:.7;pointer-events:none}
.err{margin-top:20px;background:rgba(244,33,46,.12);border:1px solid rgba(244,33,46,.3);border-radius:10px;padding:12px 16px;color:#f4212e;font-size:13px;line-height:1.5;display:none}
.setup-box{margin-top:20px;background:rgba(29,155,240,.08);border:1px solid rgba(29,155,240,.2);border-radius:12px;padding:16px;text-align:left}
.setup-box h3{color:#e7e9ea;font-size:13px;font-weight:700;margin-bottom:8px}
.setup-box p{color:#71767b;font-size:12px;line-height:1.6}
.setup-box code{background:rgba(255,255,255,.08);padding:2px 6px;border-radius:4px;font-size:11px;color:#e7e9ea}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid rgba(0,0,0,.15);border-top-color:#1d9bf0;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#120143;</div>
  <h1>X Monitor</h1>
  <p class="sub">Sign in to access your dashboard</p>

  <a class="btn-google" id="btn" href="/auth/google" onclick="loading(event)">
    <svg width="20" height="20" viewBox="0 0 24 24" style="flex-shrink:0">
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
      <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"/>
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
    </svg>
    Continue with Google
  </a>

  <div class="err" id="err"></div>
  <div class="setup-box" id="setup-box" style="display:none">
    <h3>&#9888; Google sign-in is not configured</h3>
    <p>Add your credentials to a <code>.env</code> file in the project folder:<br><br>
    <code>GOOGLE_CLIENT_ID=your_client_id</code><br>
    <code>GOOGLE_CLIENT_SECRET=your_secret</code><br><br>
    Get these from <b style="color:#e7e9ea">Google Cloud Console</b> → APIs &amp; Services → Credentials → OAuth 2.0.<br>
    Set redirect URI to: <code>http://localhost:8080/auth/google/callback</code></p>
  </div>
</div>
<script>
const _BTN_HTML = document.getElementById('btn').innerHTML;

function loading(e) {
  const btn = document.getElementById('btn');
  btn.classList.add('loading');
  btn.innerHTML = '<span class="spinner"></span> Redirecting…';
}

function resetBtn() {
  const btn = document.getElementById('btn');
  btn.classList.remove('loading');
  btn.innerHTML = _BTN_HTML;
}

// Reset button if user navigates back (bfcache restore)
window.addEventListener('pageshow', function(e) {
  if (e.persisted) resetBtn();
});

// Safety timeout — reset after 10s if redirect never happened
document.getElementById('btn').addEventListener('click', function() {
  setTimeout(resetBtn, 10000);
});

(function(){
  const err = new URLSearchParams(location.search).get('error');
  if (!err) return;
  if (err === 'setup') {
    document.getElementById('setup-box').style.display = 'block';
    return;
  }
  const el = document.getElementById('err');
  el.style.display = 'block';
  el.textContent = err === 'cancelled'
    ? 'Sign-in was cancelled. Please try again.'
    : 'Google sign-in failed — please try again.';
})();
</script>
</body>
</html>"""


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
    <div style="margin-top:10px;padding:10px 12px;background:var(--card);border:1px solid var(--border);border-radius:10px">
      <div style="font-size:12px;font-weight:700;color:var(--white)" id="app-user-chip">—</div>
      <button onclick="appLogout()" style="font-size:11px;color:var(--gray);background:none;border:none;padding:2px 0;cursor:pointer;margin-top:2px">↩ Logout</button>
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
        <button class="btn btn-ghost btn-sm" onclick="disconnectTwitter()">Disconnect</button>
      </div>
      <!-- Not connected — show manual form directly -->
      <div id="auth-form" style="padding:18px">
        <div style="font-size:13px;color:var(--gray-light);margin-bottom:14px;line-height:1.8">
          Open <b style="color:var(--white)">x.com</b> in Edge/Chrome → Press <b style="color:var(--white)">F12</b> → Go to <b style="color:var(--white)">Application</b> tab → <b style="color:var(--white)">Cookies</b> → <b style="color:var(--white)">https://x.com</b> → copy the values below:
        </div>
        <div style="display:flex;flex-direction:column;gap:10px;max-width:560px">
          <div>
            <div style="font-size:11px;color:var(--gray);margin-bottom:4px;font-weight:700;letter-spacing:.05em">AUTH_TOKEN</div>
            <input type="password" id="mc-auth" placeholder="Paste auth_token value here" style="width:100%;font-family:monospace;font-size:12px" autocomplete="off"/>
          </div>
          <div>
            <div style="font-size:11px;color:var(--gray);margin-bottom:4px;font-weight:700;letter-spacing:.05em">CT0</div>
            <input type="text" id="mc-ct0" placeholder="Paste ct0 value here" style="width:100%;font-family:monospace;font-size:12px" autocomplete="off"/>
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-top:4px">
            <button class="btn btn-blue" id="mc-btn" onclick="doManualConnect()">✅ Connect Account</button>
            <span id="mc-msg" style="font-size:12px;color:var(--gray)"></span>
          </div>
        </div>
      </div>
    </div>

    <!-- User Search & Management -->
    <div class="section" style="margin-bottom:24px">
      <div class="section-head">
        <span class="section-title">Search &amp; Add Users</span>
        <span id="count-pill" class="count-pill ok">0 / 100</span>
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
          <select id="cfg-interval" onchange="onIntervalChange(this.value)" style="width:100%">
            <option value="15">15 seconds — Very fast ⚡ (higher risk)</option>
            <option value="30" selected>30 seconds — Recommended ✅</option>
            <option value="45">45 seconds — Safe</option>
            <option value="60">60 seconds — Very safe</option>
            <option value="90">90 seconds — Safest</option>
            <option value="120">120 seconds — Maximum safe</option>
          </select>
          <div id="interval-warning" style="display:none;margin-top:8px;padding:10px 12px;background:rgba(255,112,67,.1);border:1px solid rgba(255,112,67,.3);border-radius:8px;font-size:12px;color:#ff7043;line-height:1.6">
            ⚠️ <b>15 seconds</b> is very aggressive — with many users, Twitter may rate-limit or flag your account. Please use a dedicated monitoring account.
          </div>
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
        <h3>📱 WhatsApp Alerts</h3>
        <p style="font-size:12px;color:var(--gray-light);line-height:1.6;margin-bottom:10px">
          Get a WhatsApp message the moment one of your monitored accounts posts something new.
        </p>
        <div class="form-row">
          <label class="form-label">Your WhatsApp Number</label>
          <input type="text" id="cfg-whatsapp" placeholder="+919876543210" style="width:100%">
        </div>
        <div class="settings-save-row">
          <button class="btn btn-blue btn-sm" onclick="saveWhatsApp()">💾 Save Number</button>
          <span class="save-msg" id="whatsapp-save-msg">✓ Saved!</span>
        </div>
        <div id="whatsapp-status" style="margin-top:10px;font-size:12px;color:var(--gray);line-height:1.6"></div>
      </div>

      <div class="settings-card">
        <h3>📊 Capacity</h3>
        <p style="font-size:13px;color:var(--gray-light);line-height:2">
          Max users &nbsp;<strong style="color:var(--white)" id="cap-maxusers">100</strong><br>
          Workers &nbsp;<strong style="color:var(--white)" id="cap-workers">3</strong><br>
          Per worker &nbsp;<strong style="color:var(--white)">10</strong><br>
          Cycle time &nbsp;<strong style="color:var(--white)" id="cap-interval">~30 s</strong>
        </p>
        <p style="font-size:11px;color:var(--gray);margin-top:12px">
          Added users are picked up on the next monitoring cycle.
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

  if (page === 'settings') { loadManagedUsers(); refreshAuthBadge(); loadWhatsApp(); }
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
    interval: parseInt(document.getElementById('cfg-interval').value) || 30,
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

// ── WhatsApp number ──────────────────────────────────────────────────────────
async function loadWhatsApp() {
  try {
    const d = await (await fetch('/api/whatsapp')).json();
    const input  = document.getElementById('cfg-whatsapp');
    const status = document.getElementById('whatsapp-status');
    if (input) input.value = d.number || '';
    if (status) {
      status.textContent = d.configured
        ? ''
        : '⚠️ WhatsApp sending is not set up by the admin yet — your number will be saved but alerts won\'t go out until it is.';
    }
  } catch {}
}

async function saveWhatsApp() {
  const number = (document.getElementById('cfg-whatsapp')?.value || '').trim();
  try {
    const res = await fetch('/api/whatsapp', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({number}) });
    const d = await res.json();
    if (!d.ok) { alert(d.reason || 'Could not save number'); return; }
    const msg = document.getElementById('whatsapp-save-msg');
    msg.classList.add('show');
    setTimeout(() => msg.classList.remove('show'), 2500);
  } catch {}
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
function onIntervalChange(val) {
  const w = document.getElementById('interval-warning');
  if (w) w.style.display = parseInt(val) < 30 ? 'block' : 'none';
  const ci = document.getElementById('cap-interval');
  if (ci) ci.textContent = '~' + val + ' s';
}

async function loadSettings() {
  try {
    const s = await (await fetch('/api/settings')).json();
    if (s.error) return;
    // Set dropdown — find closest option, fallback to 30
    const sel = document.getElementById('cfg-interval');
    if (sel) {
      const opts = Array.from(sel.options).map(o => parseInt(o.value));
      const closest = opts.reduce((a, b) => Math.abs(b - s.interval) < Math.abs(a - s.interval) ? b : a);
      sel.value = String(closest);
      onIntervalChange(closest);
    }
    document.getElementById('cfg-workers').value    = s.workers;
    document.getElementById('cfg-headless').checked = s.headless;
    document.getElementById('cfg-sound').checked    = s.sound;
  } catch {}
}

// ── Manage Users ───────────────────────────────────────────────────────────
let _managedUsers = [];
let _maxUsers     = 100;
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
  const atLimit = _managedUsers.length >= _maxUsers;

  const postHtml = data.latest_post_url
    ? '<a class="det-url" href="' + escAttr(data.latest_post_url) + '" target="_blank" rel="noopener"'
      + ' style="display:block;margin-top:5px;font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
      + '🔗 Latest: ' + esc(data.latest_post_url) + '</a>'
    : '';

  let action = '';
  if (already)       action = '<span class="already-tag">✓ Already added</span>';
  else if (atLimit)  action = '<span class="limit-tag">Limit reached (' + _managedUsers.length + '/' + _maxUsers + ')</span>';
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
    const maxU    = data.max_users || 100;
    _maxUsers     = maxU;
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
    const cm = document.getElementById('cap-maxusers');
    if (cm) cm.textContent = maxU;

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
    + (handle && handle !== 'Connected Account' ? '<b style="color:var(--white)">' + esc(handle) + '</b> Twitter account connected successfully.' : 'Twitter account connected successfully.')
    + '</div>'
    + '<div style="display:flex;gap:12px;justify-content:center">'
    + '<button class="btn btn-blue" id="modal-go-btn" onclick="document.getElementById(\'login-success-modal\').remove();nav(\'dashboard\')">'
    + '📊 Go to Dashboard</button>'
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
  if (btn) { btn.disabled = false; btn.textContent = '🔑 Sign in to Twitter'; }
  startLogin();
}

function renderAuthCard(d) {
  // Show/hide dashboard login banner — only the admin can act on it
  const banner = document.getElementById('login-banner');
  if (banner) banner.style.display = (d.logged_in || !d.is_admin) ? 'none' : 'flex';

  const badge = document.getElementById('auth-status-badge');
  const body  = document.getElementById('auth-body');
  if (!badge || !body) return;

  // Non-admin users never manage Twitter directly — it's shared app-wide
  if (!d.is_admin) {
    if (d.logged_in) {
      badge.textContent = '✓ Active';
      badge.style.background = 'rgba(0,186,124,.15)';
      badge.style.color = 'var(--green)';
      body.innerHTML =
        '<div style="background:rgba(0,186,124,.08);border:1px solid rgba(0,186,124,.25);border-radius:10px;padding:14px 16px;font-size:13px;color:var(--gray-light);line-height:1.6">'
        + '✓ Twitter access is managed by the app admin. You can add accounts to monitor right away below — no Twitter login needed from you.'
        + '</div>';
    } else {
      badge.textContent = 'Setting up…';
      badge.style.background = 'rgba(113,118,123,.12)';
      badge.style.color = 'var(--gray)';
      body.innerHTML =
        '<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;font-size:13px;color:var(--gray-light);line-height:1.6">'
        + 'Twitter access is managed by the app admin and is being set up. You can still add accounts to monitor below — checks will start automatically once it\'s ready.'
        + '</div>';
    }
    return;
  }

  if (d.login_in_progress) {
    _loginWasPending = true;
    badge.textContent = 'Waiting…';
    badge.style.background = 'rgba(255,112,67,.15)';
    badge.style.color = 'var(--orange)';
    body.innerHTML =
      '<div style="background:rgba(255,112,67,.08);border:1px solid rgba(255,112,67,.25);border-radius:10px;padding:16px 18px">'
      + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">'
      + '<span class="spinner" style="border-top-color:var(--orange);flex-shrink:0"></span>'
      + '<span style="font-size:14px;font-weight:700;color:var(--white)">Waiting for you to sign in via the browser…</span>'
      + '</div>'
      + '<div style="font-size:13px;color:var(--gray-light);line-height:1.6;margin-bottom:14px">'
      + '👉 An <b style="color:var(--white)">x.com</b> tab should have opened — click <b style="color:var(--green)">Sign in with Google</b> there.<br>'
      + 'This page will update automatically once you sign in.'
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
      + '<div style="font-size:13px;font-weight:700;color:var(--white);margin-bottom:4px">⚡ Instant — Import from Edge/Chrome</div>'
      + '<div style="font-size:12px;color:var(--gray-light);margin-bottom:10px">If you are already signed in to x.com in Edge or Chrome — no popup, no extra window, instant import.</div>'
      + '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
      + '<button class="btn btn-blue btn-sm" id="import-btn" onclick="importBrowserSession()">⬇ Import from Browser</button>'
      + '</div>'
      + '<div id="import-msg" style="margin-top:8px;font-size:12px;color:var(--gray)"></div>'
      + '</div>'

      // Option 2 — Open browser window (SECONDARY)
      + '<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px">'
      + '<div style="font-size:13px;font-weight:700;color:var(--gray-light);margin-bottom:4px">🌐 Sign in with Google</div>'
      + '<div style="font-size:12px;color:var(--gray);margin-bottom:10px">Your Edge/Chrome browser will open — click <b style="color:var(--white)">Sign in with Google</b> there. No password required.</div>'
      + '<button class="btn btn-ghost btn-sm" id="login-btn" onclick="startLogin()">🔑 Sign in with Google</button>'
      + '<div id="login-msg" style="margin-top:8px;font-size:12px;color:var(--gray)"></div>'
      + '</div>'

      // Option 3 — Manual cookie paste (most reliable, no dependencies)
      + '<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px">'
      + '<div style="font-size:13px;font-weight:700;color:var(--gray-light);margin-bottom:4px">🍪 Manual Cookie — Most Reliable</div>'
      + '<div style="font-size:12px;color:var(--gray);margin-bottom:8px">Paste cookies directly from Browser DevTools — no popup, no installation required.</div>'
      + '<span class="cookie-toggle" onclick="toggleManualForm()">▶ Show step-by-step guide</span>'
      + '<div id="manual-form" style="display:none;margin-top:12px">'
      + '<div class="cookie-steps">'
      + '1. Open <b>x.com</b> in your browser and sign in<br>'
      + '2. Press <b>F12</b> → <b>Application</b> tab → <b>Cookies</b> → <b>https://x.com</b><br>'
      + '3. Find <b>auth_token</b> → copy its value<br>'
      + '4. Find <b>ct0</b> → copy its value<br>'
      + '5. Paste both values below ↓'
      + '</div>'
      + '<div class="cookie-fields">'
      + '<input type="password" id="manual-auth-token" placeholder="Paste auth_token value here" autocomplete="off" />'
      + '<input type="text"     id="manual-ct0"        placeholder="Paste ct0 value here"        autocomplete="off" />'
      + '</div>'
      + '<button class="btn btn-blue btn-sm" id="manual-btn" onclick="manualLogin()">✅ Connect Account</button>'
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
  if (msg) { msg.style.color = 'var(--gray)'; msg.textContent = 'Importing cookies from Edge browser…'; }
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
      const hint = reason.toLowerCase().includes('no twitter login') || reason.toLowerCase().includes('not found')
        ? ' — please sign in to x.com in Edge browser first'
        : '';
      if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + reason + hint; }
      if (btn) { btn.disabled = false; btn.innerHTML = '2. Connect ✓'; }
    }
  } catch {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Network error — is the app running?'; }
    if (btn) { btn.disabled = false; btn.innerHTML = '2. Connect ✓'; }
  }
}

async function startLogin() {
  const btn = document.getElementById('login-btn');
  const msg = document.getElementById('login-msg');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Opening browser…'; }
  if (msg) { msg.style.color = 'var(--gray)'; msg.textContent = 'Sign in to Twitter in your browser (Google login works too) — this page will update automatically'; }
  try {
    const res = await fetch('/api/auth/login', {method:'POST'});
    const d   = await res.json();
    if (d.started) {
      _loginWasPending = true;
      loadAuthStatus();
    } else {
      if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + (d.message || 'Could not start'); }
      if (btn) { btn.disabled = false; btn.innerHTML = 'Open Browser Window'; }
    }
  } catch {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Network error'; }
    if (btn) { btn.disabled = false; btn.innerHTML = 'Open Browser Window'; }
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
  if (toggle) toggle.textContent = open ? '▼ Close guide' : '▶ Show step-by-step guide';
}

async function manualLogin() {
  const authToken = (document.getElementById('manual-auth-token')?.value || '').trim();
  const ct0       = (document.getElementById('manual-ct0')?.value || '').trim();
  const btn       = document.getElementById('manual-btn');
  const msg       = document.getElementById('manual-msg');

  if (!authToken || !ct0) {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Please fill in both fields'; }
    return;
  }
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Connecting…'; }
  if (msg) { msg.style.color = 'var(--gray)'; msg.textContent = 'Verifying cookies…'; }

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
      if (btn) { btn.disabled = false; btn.textContent = '✅ Connect Account'; }
    }
  } catch {
    if (msg) { msg.style.color = 'var(--red)'; msg.textContent = '❌ Network error'; }
    if (btn) { btn.disabled = false; btn.textContent = '✅ Connect Account'; }
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
  if (!confirm('Disconnect your Twitter account? Monitoring will continue without login.')) return;
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
  if (!authToken || !ct0) { if(msg){msg.style.color='var(--red)';msg.textContent='❌ Please fill in both fields';} return; }
  if (btn) { btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Connecting…'; }
  if (msg) { msg.style.color='var(--gray)'; msg.textContent='Verifying…'; }
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
      if (btn){btn.disabled=false; btn.textContent='✅ Connect Account';}
    }
  } catch {
    if (msg){msg.style.color='var(--red)'; msg.textContent='❌ Network error';}
    if (btn){btn.disabled=false; btn.textContent='✅ Connect Account';}
  }
}

async function doLogout() {
  if (!confirm('Disconnect your Twitter account?')) return;
  await fetch('/api/auth/logout', {method:'POST'});
  refreshAuthBadge();
}

// ── App-level logout & user chip ──────────────────────────────────────────
async function appLogout() {
  if (!confirm('Are you sure you want to log out?')) return;
  await fetch('/app/logout', {method:'POST'});
  window.location.href = '/';
}

async function loadAppUser() {
  try {
    const d = await fetch('/api/me');
    if (d.status === 401) { window.location.href = '/'; return; }
    const u = await d.json();
    const chip = document.getElementById('app-user-chip');
    if (chip) chip.textContent = u.name || u.username || '—';
  } catch {}
}

// ── Welcome / Onboarding Modal ────────────────────────────────────────────
function showWelcomeModal() {
  if (document.getElementById('welcome-modal')) return;
  const modal = document.createElement('div');
  modal.id = 'welcome-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;animation:fadein .3s ease';
  modal.innerHTML = `
<div style="background:#16181c;border:1px solid #2f3336;border-radius:20px;max-width:560px;width:100%;box-shadow:0 24px 80px rgba(0,0,0,.7);overflow:hidden">
  <div style="background:linear-gradient(135deg,rgba(29,155,240,.15),rgba(29,155,240,.05));border-bottom:1px solid #2f3336;padding:28px 32px 20px">
    <div style="font-size:32px;margin-bottom:8px">👋</div>
    <div style="font-size:22px;font-weight:900;color:#e7e9ea;margin-bottom:4px">Welcome to X Monitor</div>
    <div style="font-size:14px;color:#71767b">Here's a quick guide to get you started.</div>
  </div>
  <div style="padding:24px 32px;display:flex;flex-direction:column;gap:18px">

    <div style="display:flex;gap:14px;align-items:flex-start">
      <div style="background:rgba(29,155,240,.15);border-radius:50%;width:36px;height:36px;display:grid;place-items:center;flex-shrink:0;font-size:16px">1</div>
      <div>
        <div style="font-size:14px;font-weight:700;color:#e7e9ea;margin-bottom:3px">Connect your Twitter account</div>
        <div style="font-size:13px;color:#71767b;line-height:1.5">Go to <b style="color:#e7e9ea">Settings → Twitter Account</b>. The fastest way is <b style="color:#e7e9ea">Import from Browser</b> — it reads your existing Edge/Chrome session with one click. No password needed.</div>
      </div>
    </div>

    <div style="display:flex;gap:14px;align-items:flex-start">
      <div style="background:rgba(29,155,240,.15);border-radius:50%;width:36px;height:36px;display:grid;place-items:center;flex-shrink:0;font-size:16px">2</div>
      <div>
        <div style="font-size:14px;font-weight:700;color:#e7e9ea;margin-bottom:3px">Add accounts to monitor</div>
        <div style="font-size:13px;color:#71767b;line-height:1.5">Go to <b style="color:#e7e9ea">Manage Users</b>, type a Twitter username or display name in the search box, and click <b style="color:#e7e9ea">Add</b>. You can monitor up to <b style="color:#e7e9ea">100 accounts</b>.</div>
      </div>
    </div>

    <div style="display:flex;gap:14px;align-items:flex-start">
      <div style="background:rgba(0,186,124,.12);border-radius:50%;width:36px;height:36px;display:grid;place-items:center;flex-shrink:0;font-size:16px">⏱</div>
      <div>
        <div style="font-size:14px;font-weight:700;color:#e7e9ea;margin-bottom:3px">Check interval — 30 seconds recommended</div>
        <div style="font-size:13px;color:#71767b;line-height:1.5">In <b style="color:#e7e9ea">Settings</b>, set how often all accounts are checked. <b style="color:#e7e9ea">30 seconds</b> is the sweet spot — fast enough to catch new tweets, safe enough to avoid rate limits for up to 30 users.</div>
      </div>
    </div>

    <div style="display:flex;gap:14px;align-items:flex-start">
      <div style="background:rgba(244,33,46,.1);border-radius:50%;width:36px;height:36px;display:grid;place-items:center;flex-shrink:0;font-size:16px">⚙️</div>
      <div>
        <div style="font-size:14px;font-weight:700;color:#e7e9ea;margin-bottom:3px">Workers — browser page pool size</div>
        <div style="font-size:13px;color:#71767b;line-height:1.5">Workers control how many browser pages run in parallel. More workers = faster cycles but more RAM. <b style="color:#e7e9ea">5 workers</b> is the default and works well for most setups.</div>
      </div>
    </div>

    <div style="display:flex;gap:14px;align-items:flex-start">
      <div style="background:rgba(255,112,67,.1);border-radius:50%;width:36px;height:36px;display:grid;place-items:center;flex-shrink:0;font-size:16px">💡</div>
      <div>
        <div style="font-size:14px;font-weight:700;color:#e7e9ea;margin-bottom:3px">Use a dedicated monitoring account</div>
        <div style="font-size:13px;color:#71767b;line-height:1.5">For best results, connect a <b style="color:#e7e9ea">separate Twitter account</b> used only for monitoring — not your main account. This reduces the risk of your primary account being rate-limited.</div>
      </div>
    </div>

  </div>
  <div style="padding:16px 32px 24px;display:flex;justify-content:flex-end;gap:10px;border-top:1px solid #2f3336">
    <button class="btn btn-ghost btn-sm" onclick="dismissWelcomeModal(true)">Don't show again</button>
    <button class="btn btn-blue" onclick="dismissWelcomeModal(false)">Got it, let's start! →</button>
  </div>
</div>`;
  document.body.appendChild(modal);
}

function dismissWelcomeModal(permanent) {
  const m = document.getElementById('welcome-modal');
  if (m) m.remove();
  if (permanent) localStorage.setItem('xmon_welcome_seen', '1');
}

function maybeShowWelcome() {
  if (!localStorage.getItem('xmon_welcome_seen')) {
    setTimeout(showWelcomeModal, 600);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────
loadAppUser();
poll();
loadSettings();
loadManagedUsers();
refreshAuthBadge();
maybeShowWelcome();
setInterval(poll, 3000);
</script>
</body>
</html>"""
