"""
Twitter/X authentication via Playwright.

Login opens a visible browser window — user types their own credentials.
Cookies are saved to data/twitter_session.json and loaded by BrowserPool
on the next startup (or reconnect).

This module NEVER performs any action on behalf of the account.
It is read-only: only profile pages and search results are accessed.
"""

import asyncio
import json
import logging
import os
import threading
from typing import Optional

import config

logger = logging.getLogger(__name__)

_BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH      = os.path.join(_BASE_DIR, "data", "twitter_session.json")
SESSION_META_PATH = SESSION_PATH + ".meta"

# Twitter login is app-level and shared by every app-user (the admin connects
# one account; everyone's monitoring reuses it). A version counter — rather
# than a one-shot consume flag — lets multiple independent MonitorThreads
# (one per app-user) each detect a new session without racing each other to
# clear a single shared flag.
_session_version = 0
_version_lock = threading.Lock()


def get_session_version() -> int:
    with _version_lock:
        return _session_version


def _bump_session_version() -> None:
    global _session_version
    with _version_lock:
        _session_version += 1

# ── Session helpers ──────────────────────────────────────────────────────────

# In-memory cache so we only hit the Twitter API once per process lifetime
_cached_username: Optional[str] = None


def session_exists() -> bool:
    """True only if session file has a real auth_token cookie (not just guest cookies)."""
    if not os.path.exists(SESSION_PATH):
        return False
    try:
        with open(SESSION_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return any(c.get("name") == "auth_token" for c in data.get("cookies", []))
    except Exception:
        return False


def get_session_username() -> Optional[str]:
    """
    Returns the @handle of the logged-in account.
    If the meta file has a placeholder ("unknown" / "imported"), auto-resolves
    it from the stored session cookies — calling Twitter verify_credentials once,
    then caching both in memory and on disk so restarts don't re-fetch.
    """
    global _cached_username

    # Return cache if already resolved
    if _cached_username and _cached_username not in ("unknown", "imported"):
        return _cached_username

    # Read meta file
    if os.path.exists(SESSION_META_PATH):
        try:
            with open(SESSION_META_PATH, encoding="utf-8") as f:
                saved = json.load(f).get("username", "")
            if saved and saved not in ("unknown", "imported"):
                _cached_username = saved
                return _cached_username
        except Exception:
            pass

    # Meta is missing/placeholder — try to resolve from stored session cookies
    if os.path.exists(SESSION_PATH):
        try:
            with open(SESSION_PATH, encoding="utf-8") as f:
                cookies = json.load(f).get("cookies", [])
            username = _extract_username_from_cookies(cookies)
            if username:
                _cached_username = username
                # Persist so future restarts don't need the API call
                os.makedirs(os.path.dirname(SESSION_META_PATH), exist_ok=True)
                with open(SESSION_META_PATH, "w", encoding="utf-8") as f:
                    json.dump({"username": username}, f)
                logger.info("Auto-resolved session username: %s", username)
                return _cached_username
        except Exception as exc:
            logger.debug("Could not auto-resolve username: %s", exc)

    return None


def clear_session() -> None:
    global _cached_username
    _cached_username = None
    for path in (SESSION_PATH, SESSION_META_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    logger.info("Twitter session cleared")


def _read_locked_file(path: str) -> str:
    """
    Copy a file locked by Edge/Chrome to a temp path using Windows shared-access.
    Returns the temp file path (caller must delete).
    """
    import ctypes, ctypes.wintypes, tempfile

    k32 = ctypes.windll.kernel32
    # Must set restype to HANDLE (c_void_p) — otherwise 64-bit handles get truncated to 32-bit
    k32.CreateFileW.restype  = ctypes.c_void_p
    k32.ReadFile.restype     = ctypes.wintypes.BOOL
    k32.CloseHandle.restype  = ctypes.wintypes.BOOL
    k32.GetLastError.restype = ctypes.wintypes.DWORD

    GENERIC_READ   = 0x80000000
    FILE_SHARE_ALL = 0x7      # READ | WRITE | DELETE
    OPEN_EXISTING  = 3

    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # bypasses share-mode checks
    handle = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError(f"CreateFileW failed (err={k32.GetLastError()}): {path}")

    try:
        size = os.path.getsize(path)
        if size == 0:
            raise OSError("File is empty")
        chunk   = 1024 * 1024   # 1 MB chunks
        chunks  = []
        read    = ctypes.wintypes.DWORD(0)
        while True:
            buf = ctypes.create_string_buffer(min(chunk, size - sum(len(c) for c in chunks) + chunk))
            ok  = k32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
            if not ok or read.value == 0:
                break
            chunks.append(buf.raw[:read.value])
            if sum(len(c) for c in chunks) >= size:
                break
        data = b"".join(chunks)
        if not data:
            raise OSError(f"ReadFile returned no data (err={k32.GetLastError()})")
    finally:
        k32.CloseHandle(handle)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


def _read_edge_cookies_dpapi() -> list[dict]:
    """
    Read Edge/Chrome cookies directly via SQLite + Windows DPAPI.
    Uses Windows shared-access file reading to bypass Edge's file lock.
    Windows-only.
    """
    import ctypes
    import ctypes.wintypes
    import sqlite3
    import base64

    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        ("Edge",   os.path.join(local_app, "Microsoft", "Edge",   "User Data")),
        ("Chrome", os.path.join(local_app, "Google",   "Chrome",  "User Data")),
    ]

    for _name, base in candidates:
        local_state_path = os.path.join(base, "Local State")
        if not os.path.exists(local_state_path):
            continue

        # ── 1. Decrypt master AES key with Windows DPAPI ────────────────
        try:
            import json as _json
            with open(local_state_path, "r", encoding="utf-8") as _f:
                ls = _json.load(_f)
            enc_key = base64.b64decode(ls.get("os_crypt", {}).get("encrypted_key", ""))
            if enc_key[:5] == b"DPAPI":
                enc_key = enc_key[5:]

            class _BLOB(ctypes.Structure):
                _fields_ = [("cbData", ctypes.wintypes.DWORD),
                             ("pbData", ctypes.POINTER(ctypes.c_char))]

            inp = _BLOB(len(enc_key),
                        ctypes.cast(ctypes.c_char_p(enc_key), ctypes.POINTER(ctypes.c_char)))
            out = _BLOB()
            if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)
            ):
                continue
            key = ctypes.string_at(out.pbData, out.cbData)
            ctypes.windll.kernel32.LocalFree(out.pbData)
        except Exception as exc:
            logger.debug("DPAPI key decrypt failed for %s: %s", _name, exc)
            continue

        # ── 2. Copy Cookies SQLite via Windows shared-access read ────────
        result: list[dict] = []
        for suffix in [os.path.join("Default", "Network", "Cookies"),
                       os.path.join("Default", "Cookies")]:
            db = os.path.join(base, suffix)
            if not os.path.exists(db):
                continue
            tmp_path = None
            try:
                tmp_path = _read_locked_file(db)
                con = sqlite3.connect(tmp_path)
                rows = con.execute(
                    "SELECT host_key,name,encrypted_value,path,secure,expires_utc FROM cookies "
                    "WHERE host_key LIKE '%.x.com' OR host_key LIKE '%.twitter.com'"
                ).fetchall()
                con.close()
            except Exception as exc:
                logger.debug("SQLite open failed %s: %s", db, exc)
                continue
            finally:
                if tmp_path:
                    try: os.remove(tmp_path)
                    except: pass

            # ── 3. Decrypt each cookie value (AES-256-GCM, v10 prefix) ─
            for host, name, enc_val, path, secure, exp_utc in rows:
                try:
                    from Cryptodome.Cipher import AES as _AES  # type: ignore
                    if enc_val[:3] != b"v10":
                        continue
                    nonce, ctxt, tag = enc_val[3:15], enc_val[15:-16], enc_val[-16:]
                    value = _AES.new(key, _AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ctxt, tag).decode()
                except Exception:
                    continue

                domain = host if host.startswith(".") else "." + host
                exp_unix = int(exp_utc // 1_000_000 - 11_644_473_600) if exp_utc > 0 else -1
                result.append({
                    "name": name, "value": value, "domain": domain,
                    "path": path or "/", "secure": bool(secure),
                    "httpOnly": False, "sameSite": "None", "expires": exp_unix,
                })

            if result:
                return result

    return []


def _extract_username_from_cookies(cookies: list[dict]) -> Optional[str]:
    """
    Try to pull the Twitter @handle from imported cookies.
    1. Check for a screen_name cookie (rare but possible).
    2. Make a quick API call to /1.1/account/verify_credentials.json using
       the auth_token + ct0 from the cookie list (most reliable).
    Returns "@handle" or None.
    """
    # Fast path: screen_name cookie (present in some legacy sessions)
    for c in cookies:
        if c["name"] == "screen_name" and c.get("value"):
            return "@" + c["value"].lstrip("@")

    # API path: use auth_token + ct0 to ask Twitter who we are
    auth_token = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
    ct0        = next((c["value"] for c in cookies if c["name"] == "ct0"), None)
    if not auth_token or not ct0:
        return None

    try:
        import urllib.request
        import json as _json
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies
            if c["name"] in ("auth_token", "ct0", "twid", "guest_id")
        )
        req = urllib.request.Request(
            "https://api.twitter.com/1.1/account/verify_credentials.json",
            headers={
                "Authorization":  "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
                "Cookie":         cookie_header,
                "x-csrf-token":   ct0,
                "x-twitter-auth-type": "OAuth2Session",
                "User-Agent":     "TwitterAndroid/9.95.0-release.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        sn = data.get("screen_name")
        if sn:
            return "@" + sn
    except Exception as exc:
        logger.debug("verify_credentials failed: %s", exc)

    return None


def import_from_browser() -> tuple[bool, str]:
    """
    Read Twitter cookies from the user's installed browser (Edge or Chrome)
    and save them as a Playwright storage-state file.
    Auto-installs browser-cookie3 if not present.
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        import subprocess, sys
        logger.info("browser-cookie3 not found — installing automatically…")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "browser-cookie3==0.19.1"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return False, f"Install failed: {result.stderr.strip()[-200:]}"
        except Exception as exc:
            return False, f"Could not install: {exc}"
        try:
            import browser_cookie3  # type: ignore  # noqa: F811
        except ImportError:
            return False, "Import still failing after install — please restart the app"

    cookies_list: list[dict] = []
    imported_from = ""

    # ── Try browser_cookie3 first (works when browser is closed) ────────
    for browser_name, loader in [("Edge", browser_cookie3.edge), ("Chrome", browser_cookie3.chrome)]:
        raw: list[dict] = []
        for domain in (".x.com", ".twitter.com"):
            try:
                for c in loader(domain_name=domain):
                    raw.append({
                        "name":     c.name,
                        "value":    c.value,
                        "domain":   c.domain if c.domain.startswith(".") else "." + c.domain,
                        "path":     c.path or "/",
                        "secure":   bool(c.secure),
                        "httpOnly": False,
                        "sameSite": "None",
                        "expires":  int(c.expires) if c.expires else -1,
                    })
            except Exception as exc:
                logger.debug("browser_cookie3 %s/%s: %s", browser_name, domain, exc)

        if any(c["name"] == "auth_token" for c in raw):
            cookies_list = raw
            imported_from = browser_name
            break

    # ── Fallback: direct SQLite+DPAPI read (works when Edge is running) ──
    if not cookies_list:
        try:
            raw = _read_edge_cookies_dpapi()
            if any(c["name"] == "auth_token" for c in raw):
                cookies_list = raw
                imported_from = "Edge (direct)"
                logger.info("Cookies imported via direct DPAPI method")
        except Exception as exc:
            logger.debug("Direct DPAPI read failed: %s", exc)

    if not cookies_list or not any(c["name"] == "auth_token" for c in cookies_list):
        return False, (
            "No Twitter login found in Edge/Chrome. "
            "Please sign in to x.com in your browser first, then try again."
        )

    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
    with open(SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies_list, "origins": []}, f)

    # Try to extract username from twid cookie (u%3D{user_id}) or screen_name cookie
    username = _extract_username_from_cookies(cookies_list)
    with open(SESSION_META_PATH, "w", encoding="utf-8") as f:
        json.dump({"username": username or "imported"}, f)

    global _cached_username
    _cached_username = None  # force re-resolve on next get_session_username() call
    _bump_session_version()

    logger.info("Twitter session imported from %s (%d cookies)", imported_from, len(cookies_list))
    return True, imported_from


def save_manual_cookies(auth_token: str, ct0: str) -> tuple[bool, str]:
    """
    Save manually entered Twitter cookies (auth_token + ct0).
    User copies these from browser DevTools → Application → Cookies → x.com.
    """
    auth_token = auth_token.strip()
    ct0        = ct0.strip()
    if not auth_token or not ct0:
        return False, "auth_token aur ct0 dono required hain"

    cookies_list = [
        {"name": "auth_token", "value": auth_token, "domain": ".x.com",
         "path": "/", "secure": True, "httpOnly": True, "sameSite": "None", "expires": -1},
        {"name": "auth_token", "value": auth_token, "domain": ".twitter.com",
         "path": "/", "secure": True, "httpOnly": True, "sameSite": "None", "expires": -1},
        {"name": "ct0", "value": ct0, "domain": ".x.com",
         "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": -1},
        {"name": "ct0", "value": ct0, "domain": ".twitter.com",
         "path": "/", "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": -1},
    ]

    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
    with open(SESSION_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies_list, "origins": []}, f)

    username = _extract_username_from_cookies(cookies_list)
    with open(SESSION_META_PATH, "w", encoding="utf-8") as f:
        json.dump({"username": username or "unknown"}, f)

    global _cached_username
    _cached_username = None
    _bump_session_version()

    logger.info("Manual cookies saved — user: %s", username)
    return True, username or "unknown"


# ── Login state (shared between login thread and web endpoints) ──────────────

_state: dict = {
    "in_progress": False,
    "done":        False,
    "result":      None,   # {"ok": bool, "username": str} or {"ok": False, "reason": str}
}
_state_lock = threading.Lock()


def get_login_state() -> dict:
    with _state_lock:
        return dict(_state)


def _set_state(**kw: object) -> None:
    with _state_lock:
        _state.update(kw)


# ── Core login coroutine (runs in its own event loop / thread) ───────────────

# Persistent browser profile dir — Google stays logged in across restarts
_LOGIN_PROFILE_DIR = os.path.join(_BASE_DIR, "data", "login_profile")


async def _do_login_async() -> dict:
    """
    Open a visible Chromium window with a PERSISTENT profile so Google OAuth
    works with one click after the first login.
    """
    from playwright.async_api import async_playwright

    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
    os.makedirs(_LOGIN_PROFILE_DIR, exist_ok=True)

    async with async_playwright() as p:
        # launch_persistent_context keeps cookies/Google session between logins
        context = await p.chromium.launch_persistent_context(
            user_data_dir=_LOGIN_PROFILE_DIR,
            headless=False,
            no_viewport=True,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-popup-blocking",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        # Track Google OAuth popups
        _popup_pages: list = []
        context.on("page", lambda p: _popup_pages.append(p))

        pages = context.pages
        page = pages[0] if pages else await context.new_page()
        logger.info("Opening Twitter login page (persistent profile)…")

        # Notify the user
        try:
            from winotify import Notification
            n = Notification(
                app_id="X Monitor",
                title="Twitter Login — Browser Window Opened",
                msg="Sign in to Twitter in the browser window that just opened. Google login also works.",
                duration="long",
            )
            n.show()
        except Exception:
            pass

        await page.goto("https://x.com/i/flow/login")   # direct login page

        # Wait up to 3 minutes for login to complete.
        # Most reliable check: auth_token cookie appears only when truly logged in.
        login_done = False
        deadline = asyncio.get_event_loop().time() + 180
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            if page.is_closed():
                break
            try:
                cookies = await context.cookies(["https://x.com", "https://twitter.com"])
                if any(c["name"] == "auth_token" for c in cookies):
                    login_done = True
                    break
            except Exception:
                pass  # context not ready yet — keep polling

        if not login_done:
            try:
                await context.close()
            except Exception:
                pass
            return {"ok": False, "reason": "Login timed out or window was closed"}

        await asyncio.sleep(2)

        # Wait for nav to fully render before extracting username
        try:
            await page.wait_for_selector('[data-testid="AppTabBar_Profile_Link"]', timeout=8000)
        except Exception:
            pass

        # Try to read logged-in username from the page
        username: Optional[str] = None
        for js in [
            # Method 1: profile link in nav sidebar
            """() => {
                const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
                if (a) { const m = a.href.match(/x\\.com\\/([^/?#]+)/); if (m && m[1] !== 'home') return '@' + m[1]; }
                return null;
            }""",
            # Method 2: from React store injected in scripts
            """() => {
                for (const s of document.querySelectorAll('script')) {
                    const m = s.textContent.match(/"screen_name":"([^"]+)"/);
                    if (m) return '@' + m[1];
                }
                return null;
            }""",
            # Method 3: account settings link
            """() => {
                const links = document.querySelectorAll('a[href^="/"][role="link"]');
                for (const a of links) {
                    const m = a.href.match(/x\\.com\\/([^/?#]+)$/);
                    if (m && !['home','explore','notifications','messages','i'].includes(m[1]))
                        return '@' + m[1];
                }
                return null;
            }""",
        ]:
            try:
                val = await page.evaluate(js)
                if val and val.startswith("@") and val != "@unknown":
                    username = val
                    break
            except Exception:
                continue

        # Method 4: navigate to settings to get username from URL
        if not username:
            try:
                await page.goto("https://x.com/settings/profile", wait_until="domcontentloaded", timeout=10000)
                await asyncio.sleep(1)
                # Click on profile tab in nav
                el = await page.query_selector('[data-testid="AppTabBar_Profile_Link"]')
                if el:
                    href = await el.get_attribute("href") or ""
                    import re as _re
                    m = _re.search(r'x\.com/([^/?#]+)', href)
                    if m and m.group(1) not in ("home", "i", "explore"):
                        username = "@" + m.group(1)
            except Exception:
                pass

        # Save browser storage state (cookies + localStorage)
        await context.storage_state(path=SESSION_PATH)

        # Save metadata
        with open(SESSION_META_PATH, "w", encoding="utf-8") as f:
            json.dump({"username": username or "unknown"}, f)

        # Signal MonitorThread(s) to reload the browser pool with the new session
        _bump_session_version()

        await context.close()
        logger.info("Login successful — session saved. User: %s", username)
        return {"ok": True, "username": username or "unknown"}


# ── Public: start login in background thread ─────────────────────────────────

def start_login() -> tuple[bool, str]:
    """
    Open x.com/login in the user's own Chrome/Edge browser (Google login works there),
    then poll every 3 s for the auth_token cookie — import as soon as it appears.
    Non-blocking — caller polls get_login_state() for completion.
    """
    with _state_lock:
        if _state["in_progress"]:
            return False, "Login already in progress"
        _state.update({"in_progress": True, "done": False, "result": None})

    import webbrowser
    webbrowser.open("https://x.com/login")

    def _poll() -> None:
        import time
        deadline = time.time() + 300   # wait up to 5 minutes
        while time.time() < deadline:
            time.sleep(3)
            # Check if cancelled
            with _state_lock:
                if not _state["in_progress"]:
                    return
            # Try importing cookies from browser
            try:
                ok, src = import_from_browser()
                if ok:
                    uname = get_session_username() or "unknown"
                    _set_state(in_progress=False, done=True,
                               result={"ok": True, "username": uname})
                    logger.info("Login successful via %s — user: %s", src, uname)
                    return
            except Exception:
                pass
        _set_state(in_progress=False, done=True,
                   result={"ok": False, "reason": "Timeout — login was not completed within 5 minutes"})

    t = threading.Thread(target=_poll, daemon=True, name="twitter-login")
    t.start()
    return True, "x.com/login has been opened in your browser — sign in with Google"
