"""
Core profile-check logic.
Navigates to x.com/<username>, extracts the latest post ID and URL.

Scraping strategy
-----------------
1. Load the profile page (domcontentloaded is enough — tweets are in the HTML).
2. Dismiss any login/cookie modal that overlays the content.
3. Wait for at least one article[data-testid="tweet"] to appear.
4. Collect every  a[href*="/status/"]  whose href matches
       /{username}/status/{digits}
   (this filters out retweets of other users, quoted sources, etc.)
5. Return the PostInfo with the highest tweet-ID (= most recent tweet).
6. Parse display name from the page title:
       "Display Name (@handle) / X"
"""

import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

import config
from browser import BrowserPool
from models import MonitorResult, PostInfo

logger = logging.getLogger(__name__)

_STATUS_RE = re.compile(r"/([^/]+)/status/(\d+)", re.IGNORECASE)
_TITLE_RE = re.compile(r"^(.+?)\s*\(@[^)]+\)")
_TIMELINE_API = "https://api.twitter.com/1.1/statuses/user_timeline.json"


class ProfileMonitor:
    """Scrapes a single X profile page to detect the latest post."""

    def __init__(self, browser_pool: BrowserPool) -> None:
        self._pool = browser_pool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_user(self, username: str) -> MonitorResult:
        """
        Return the latest post for *username*.

        Fast path  — Twitter's v1.1 JSON API (bypasses bot detection, ~10× faster).
        Slow path  — Playwright browser scraping (fallback when API has no session).
        Always returns a MonitorResult; never raises.
        """
        # ── Fast path: direct Twitter API call ──────────────────────────
        try:
            post = await self._fetch_via_api(username)
            if post is not None:
                logger.debug("API: @%s → post %s", username, post.post_id)
                return MonitorResult(username=username, success=True, post=post)
            logger.debug("API returned nothing for @%s — trying browser", username)
        except Exception as exc:
            logger.debug("API path error for @%s: %s", username, exc)

        # ── Slow path: browser scraping ──────────────────────────────────
        last_error: Optional[str] = None
        for attempt in range(1, config.MAX_RETRIES + 2):
            try:
                async with self._pool.lease() as page:
                    post = await self._fetch_latest_post(page, username)
                return MonitorResult(username=username, success=True, post=post)

            except PlaywrightTimeout as exc:
                last_error = f"Timeout ({exc})"
                logger.warning(
                    "Timeout @%s (attempt %d/%d)", username, attempt, config.MAX_RETRIES + 1
                )
            except Exception as exc:
                last_error = str(exc)
                logger.error(
                    "Error @%s (attempt %d/%d): %s",
                    username, attempt, config.MAX_RETRIES + 1, exc,
                )
            if attempt <= config.MAX_RETRIES:
                await asyncio.sleep(config.RETRY_WAIT_SECONDS)

        logger.error("Giving up on @%s after %d attempts", username, config.MAX_RETRIES + 1)
        return MonitorResult(username=username, success=False, error=last_error)

    # ------------------------------------------------------------------
    # Fast path: Twitter JSON API
    # ------------------------------------------------------------------

    async def _fetch_via_api(self, username: str) -> Optional[PostInfo]:
        """
        Call Twitter's v1.1 user_timeline endpoint via the browser context's
        authenticated session.  No page navigation — pure JSON, zero bot detection.
        Returns the most recent PostInfo or None if the API is unavailable.
        """
        data = await self._pool.api_get(
            _TIMELINE_API,
            params={
                "screen_name": username,
                "count": 5,
                "exclude_replies": "false",
                "include_rts": "false",
                "tweet_mode": "extended",
            },
        )
        if not data or not isinstance(data, list):
            return None

        tweet = data[0]
        tweet_id: str = tweet.get("id_str", "")
        if not tweet_id:
            return None

        display_name: Optional[str] = (tweet.get("user") or {}).get("name")
        return PostInfo(
            post_id=tweet_id,
            post_url=f"https://x.com/{username}/status/{tweet_id}",
            username=username,
            display_name=display_name,
        )

    # ------------------------------------------------------------------
    # Slow path: Playwright browser scraping
    # ------------------------------------------------------------------

    async def _fetch_latest_post(self, page: Page, username: str) -> Optional[PostInfo]:
        """
        Load the profile page and return a PostInfo for the newest tweet.
        Returns None if the account is suspended, private, or has no posts.
        """
        profile_url = f"{config.X_BASE_URL}/{username}"

        # Load page, then wait for network to settle so React/XHR tweets render
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        except PlaywrightTimeout:
            logger.debug("goto timeout for @%s, continuing anyway", username)

        # Wait for XHR/React to finish loading tweet content
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeout:
            pass

        await asyncio.sleep(2.0)

        # Detect hard redirect to login page
        current_url = page.url
        if "login" in current_url and username.lower() not in current_url.lower():
            logger.warning(
                "@%s — Twitter login wall detected. "
                "Please connect a Twitter account in Settings.",
                username,
            )
            return None

        # Dismiss overlays — run twice (first pass may reveal second layer)
        await self._dismiss_overlays(page)
        await asyncio.sleep(0.8)
        await self._dismiss_overlays(page)

        # Try Escape key to close any remaining modal
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # Detect soft login wall (overlay over content without URL change)
        await self._dismiss_soft_login_wall(page)

        # Check for "this account doesn't exist / is suspended" pages
        if await self._is_unavailable(page):
            logger.warning("@%s — account unavailable or protected", username)
            return None

        # Wait for tweet links to appear
        found = False
        try:
            await page.wait_for_selector('a[href*="/status/"]', timeout=config.PAGE_TIMEOUT_MS)
            found = True
        except PlaywrightTimeout:
            pass

        if not found:
            # Scroll down in steps to trigger lazy-loaded tweets
            for scroll in (400, 800, 1400):
                await page.evaluate(f"window.scrollTo(0, {scroll})")
                await asyncio.sleep(1.5)
                links = await page.query_selector_all('a[href*="/status/"]')
                if links:
                    found = True
                    break

        if not found:
            # JS-based extraction as final fallback (catches dynamically injected links)
            try:
                hrefs = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]'))"
                    ".map(a => a.getAttribute('href'))"
                    ".filter(h => h && h.includes('/status/'))"
                )
                if hrefs:
                    found = True
            except Exception:
                pass

        if not found:
            try:
                body_text = (await page.inner_text("body"))
                body_snippet = body_text[:200].replace("\n", " ")
                logger.warning("Timeline did not load for @%s — page snippet: %s", username, body_snippet)
                # "hasn't posted" = Posts tab empty (account only has replies, or bot detection)
                # Try /with_replies tab which shows ALL activity
                if "hasn't posted" in body_text.lower() or "when they do" in body_text.lower():
                    logger.info("Trying with_replies tab for @%s", username)
                    return await self._fetch_from_with_replies(page, username)
            except Exception:
                logger.warning("Timeline did not load for @%s", username)
            return None

        post_url = await self._find_latest_post_url(page, username)
        if not post_url:
            logger.warning("No own posts found in timeline for @%s", username)
            return None

        post_id = self._extract_post_id(post_url)
        if not post_id:
            return None

        display_name = await self._get_display_name(page, username)

        return PostInfo(
            post_id=post_id,
            post_url=post_url,
            username=username,
            display_name=display_name,
        )

    # ------------------------------------------------------------------
    # DOM helpers
    # ------------------------------------------------------------------

    async def _fetch_from_with_replies(self, page: Page, username: str) -> Optional[PostInfo]:
        """
        Fallback: load /{username}/with_replies which shows all tweets + replies.
        Used when the Posts tab shows empty state due to reply-only accounts or
        bot-detection filtering.
        """
        # Re-try API before browser (reply-only accounts still appear in user_timeline)
        try:
            post = await self._fetch_via_api(username)
            if post is not None:
                logger.debug("API (retry) found post for @%s: %s", username, post.post_id)
                return post
        except Exception:
            pass

        url = f"{config.X_BASE_URL}/{username}/with_replies"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        except PlaywrightTimeout:
            logger.debug("goto timeout for @%s/with_replies, continuing", username)

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeout:
            pass

        await asyncio.sleep(2.0)
        await self._dismiss_overlays(page)
        await self._dismiss_soft_login_wall(page)

        try:
            await page.wait_for_selector('a[href*="/status/"]', timeout=8000)
        except PlaywrightTimeout:
            pass

        # Extra scroll to trigger lazy-loading
        for scroll in (400, 800):
            await page.evaluate(f"window.scrollTo(0, {scroll})")
            await asyncio.sleep(0.8)

        post_url = await self._find_latest_post_url(page, username)
        if not post_url:
            try:
                snip = (await page.inner_text("body"))[:200].replace("\n", " ")
                logger.warning("No posts in with_replies tab for @%s — snippet: %s", username, snip)
            except Exception:
                logger.warning("No posts in with_replies tab for @%s", username)
            return None

        post_id = self._extract_post_id(post_url)
        if not post_id:
            return None

        display_name = await self._get_display_name(page, username)
        return PostInfo(
            post_id=post_id,
            post_url=post_url,
            username=username,
            display_name=display_name,
        )

    async def _dismiss_overlays(self, page: Page) -> None:
        """
        Silently close modal dialogs (login wall, cookie consent)
        that appear over the timeline on the first visit.
        """
        # Try both React (data-testid) and SSR (role/aria) selectors
        close_selectors = [
            '[data-testid="app-bar-close"]',
            '[role="button"][aria-label*="Close"]',
            '[role="button"][aria-label*="close"]',
            'button[aria-label*="Close"]',
        ]
        for selector in close_selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click(timeout=2000)
                    await asyncio.sleep(0.5)
            except Exception:
                pass

        # Accept cookie / GDPR banners if present
        for text in ("Accept all", "Accept All Cookies", "Agree"):
            try:
                btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
                if await btn.is_visible():
                    await btn.click(timeout=2000)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

    async def _dismiss_soft_login_wall(self, page: Page) -> None:
        """
        Twitter shows a soft login gate (modal over content) without changing
        the URL. Detect it by body text and try to close or bypass it.
        """
        soft_wall_phrases = [
            "sign in to x", "log in to x",
            "don't miss what's happening",
            "new to x?", "sign up now",
        ]
        try:
            body_text = (await page.inner_text("body")).lower()
            if not any(p in body_text for p in soft_wall_phrases):
                return
        except Exception:
            return

        # Try closing any visible dialog/sheet
        for sel in [
            '[data-testid="sheetDialog"] [data-testid="app-bar-close"]',
            'div[role="dialog"] [aria-label*="Close"]',
            'div[role="dialog"] [aria-label*="close"]',
            '[data-testid="mask"]',
            'div[data-testid="BottomBar"]',
        ]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click(timeout=2000)
                    await asyncio.sleep(0.6)
                    return
            except Exception:
                pass

        # Press Escape to close any modal
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            pass

        # Scroll past the wall in steps — reveals content below
        try:
            for offset in (300, 600, 1000):
                await page.evaluate(f"window.scrollTo(0, {offset})")
                await asyncio.sleep(0.6)
            # Scroll back to top so tweet links are in DOM
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    async def _is_unavailable(self, page: Page) -> bool:
        """Return True if X shows a 'not found / suspended / protected' error."""
        unavailable_phrases = [
            "account suspended",
            "this account doesn't exist",
            "caution: this account is temporarily restricted",
            "these tweets are protected",
        ]
        try:
            body_text = (await page.inner_text("body")).lower()
            return any(phrase in body_text for phrase in unavailable_phrases)
        except Exception:
            return False

    async def _find_latest_post_url(self, page: Page, username: str) -> Optional[str]:
        """
        Scan all status links in tweet articles and return the URL whose
        numeric tweet ID is the highest (= most recently published).

        Retweets (links to another user's post) are excluded by matching
        only links whose path starts with /{username}/.
        """
        pattern = re.compile(
            rf"/{re.escape(username)}/status/(\d+)",
            re.IGNORECASE,
        )

        # Get all status hrefs — try JS first (faster & catches dynamically injected links)
        try:
            raw_hrefs: list[str] = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.getAttribute('href'))"
                ".filter(h => h && h.includes('/status/'))"
            )
        except Exception:
            raw_hrefs = []

        # Fallback to Playwright element query if JS returned nothing
        if not raw_hrefs:
            elements = await page.query_selector_all('a[href*="/status/"]')
            raw_hrefs = []
            for el in elements:
                try:
                    h = await el.get_attribute("href")
                    if h:
                        raw_hrefs.append(h)
                except Exception:
                    pass

        best_id: Optional[int] = None
        best_url: Optional[str] = None

        for href in raw_hrefs:
            if not href:
                continue

            m = pattern.search(href)
            if not m:
                continue

            tweet_id = int(m.group(1))
            if best_id is None or tweet_id > best_id:
                best_id = tweet_id
                best_url = (
                    f"https://x.com{href}" if href.startswith("/") else href
                )

        return best_url

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_post_id(self, post_url: str) -> Optional[str]:
        """Parse the numeric tweet ID from a post URL."""
        m = _STATUS_RE.search(post_url)
        return m.group(2) if m else None

    async def _get_display_name(self, page: Page, username: str) -> Optional[str]:
        """
        Extract the display name from the loaded profile page.

        Primary:   page title  →  "Display Name (@handle) / X"
        Fallback:  first tweet's author span
        """
        # --- Strategy 1: page title (most reliable) ---
        try:
            title = await page.title()
            m = _TITLE_RE.match(title)
            if m:
                name = m.group(1).strip()
                if name:
                    return name
        except Exception:
            pass

        # --- Strategy 2: profile header UserName testid ---
        header_selectors = [
            f'[data-testid="UserName"] span:not(:has(span))',
            'h2[data-testid="UserName"] span',
        ]
        for selector in header_selectors:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and not text.startswith("@"):
                        return text
            except Exception:
                continue

        return None
