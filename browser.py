"""
Playwright browser pool manager.
One Chromium instance is shared across all workers via a page semaphore.
Workers acquire a page via acquire_page() / release_page() or the
async context manager PageLease.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

# Twitter web-app bearer token (same one embedded in x.com's JS bundle)
_TWITTER_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

import config

logger = logging.getLogger(__name__)


class BrowserPool:
    """
    Manages a single headless Chromium browser with a fixed pool of
    reusable pages.  Workers call acquire_page() to borrow a page and
    release_page() when done.  If a page crashes it is transparently
    replaced before being returned.
    """

    def __init__(self, pool_size: int = config.WORKER_COUNT,
                 session_path: Optional[str] = None) -> None:
        self._pool_size   = pool_size
        self._session_path = session_path  # path to twitter_session.json (or None)
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        # Queue acts as a counting semaphore + FIFO page store
        self._pages: asyncio.Queue[Page] = asyncio.Queue()
        self._lock = asyncio.Lock()          # guards browser (re)launch
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch Chromium and pre-create the page pool."""
        self._playwright = await async_playwright().start()
        await self._launch_browser()
        self._running = True
        logger.info(
            "Browser pool started — %d page(s), headless=%s",
            self._pool_size,
            config.HEADLESS,
        )

    async def stop(self) -> None:
        """Close all pages, context, browser, and Playwright cleanly."""
        self._running = False
        # Drain pages from queue and close them
        while not self._pages.empty():
            try:
                page = self._pages.get_nowait()
                await page.close()
            except Exception:
                pass

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        logger.info("Browser pool stopped")

    async def _launch_browser(self) -> None:
        """(Re)launch browser and fill the page pool — called on startup
        and after crash recovery."""
        assert self._playwright is not None

        # Close stale context/browser if this is a relaunch
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass

        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--disable-default-apps",
        ]
        # Prefer system Edge (less bot-detectable than bundled Chromium)
        try:
            self._browser = await self._playwright.chromium.launch(
                channel="msedge",
                headless=config.HEADLESS,
                args=launch_args,
            )
            logger.info("Monitoring browser: Microsoft Edge")
        except Exception:
            self._browser = await self._playwright.chromium.launch(
                headless=config.HEADLESS,
                args=launch_args,
            )
            logger.info("Monitoring browser: bundled Chromium")

        ctx_kwargs: dict = dict(
            user_agent=config.USER_AGENT,
            viewport={"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT},
            java_script_enabled=True,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        if self._session_path and os.path.exists(self._session_path):
            ctx_kwargs["storage_state"] = self._session_path
            logger.info("Browser context loaded with saved Twitter session")

        self._context = await self._browser.new_context(**ctx_kwargs)

        # Block resource types that are irrelevant to scraping text
        await self._context.route(
            "**/*",
            self._block_heavy_resources,
        )

        # Clear the queue before refilling
        while not self._pages.empty():
            try:
                self._pages.get_nowait()
            except asyncio.QueueEmpty:
                break

        for _ in range(self._pool_size):
            page = await self._build_page()
            await self._pages.put(page)

        logger.debug("Browser (re)launched, %d pages ready", self._pool_size)

    # ------------------------------------------------------------------
    # Resource blocking
    # ------------------------------------------------------------------

    @staticmethod
    async def _block_heavy_resources(route, request) -> None:  # type: ignore[no-untyped-def]
        """Abort image, font, media, and tracking requests to save bandwidth."""
        blocked_types = {"image", "font", "media", "websocket"}
        blocked_domains = (
            "google-analytics.com",
            "doubleclick.net",
            "ads.twitter.com",
        )
        if request.resource_type in blocked_types:
            await route.abort()
            return
        if any(d in request.url for d in blocked_domains):
            await route.abort()
            return
        await route.continue_()

    # ------------------------------------------------------------------
    # Page pool
    # ------------------------------------------------------------------

    async def acquire_page(self) -> Page:
        """
        Borrow a page from the pool.  Blocks until one is available.
        If the page has crashed it is replaced before being returned.
        """
        page = await self._pages.get()

        # Verify the page is still alive; replace if not
        if page.is_closed():
            logger.warning("Acquired a closed page — rebuilding")
            try:
                page = await self._rebuild_page()
            except Exception as exc:
                logger.error("Page rebuild failed: %s — restarting browser", exc)
                async with self._lock:
                    await self._launch_browser()
                page = await self._pages.get()  # get fresh page from refilled pool

        return page

    async def release_page(self, page: Page) -> None:
        """Return a page to the pool after use."""
        if page.is_closed():
            # Page died during use — put a fresh replacement back
            logger.warning("Released page was closed — replacing")
            try:
                page = await self._rebuild_page()
            except Exception as exc:
                logger.error("Cannot rebuild page after release: %s", exc)
                # Attempt full browser restart to keep pool size correct
                async with self._lock:
                    await self._launch_browser()
                return  # _launch_browser already refilled the queue
        await self._pages.put(page)

    async def _rebuild_page(self) -> Page:
        """Create a single fresh configured page in the existing context."""
        if self._context is None:
            raise RuntimeError("Browser context is not available")
        return await self._build_page()

    async def _build_page(self) -> Page:
        """Open a new page and apply standard settings."""
        assert self._context is not None
        page = await self._context.new_page()

        # Suppress automation-detection signals
        await page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
            window.chrome = {
                app: { isInstalled: false },
                runtime: {
                    PlatformOs: { WIN: 'win' },
                    PlatformArch: { X86_64: 'x86-64' },
                    OnInstalledReason: { INSTALL: 'install', UPDATE: 'update' },
                },
            };
            const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : _origQuery(p);
            """
        )

        page.set_default_timeout(config.PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(config.PAGE_TIMEOUT_MS)
        return page

    # ------------------------------------------------------------------
    # Async context manager for single-page lease
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[Page]:
        """
        Async context manager that acquires a page and guarantees release.

        Usage::

            async with pool.lease() as page:
                await page.goto(url)
        """
        page = await self.acquire_page()
        try:
            yield page
        finally:
            await self.release_page(page)

    # ------------------------------------------------------------------
    # Twitter API helper (uses browser context cookies — no page needed)
    # ------------------------------------------------------------------

    async def api_get(
        self, url: str, params: Optional[dict] = None
    ) -> "Any":
        """
        Authenticated GET to a Twitter/X API endpoint.

        Uses the browser context's session cookies (auth_token + ct0) so
        the request is treated as coming from a logged-in web session.
        Returns parsed JSON (list or dict) or None on any error.
        """
        if not self._context:
            return None
        try:
            raw = await self._context.cookies(["https://x.com", "https://twitter.com"])
            ct0 = next((c["value"] for c in raw if c["name"] == "ct0"), "")
            if not ct0:
                logger.debug("api_get: ct0 cookie missing — session not loaded")
                return None

            resp = await self._context.request.get(
                url,
                params=params or {},
                headers={
                    "Authorization": f"Bearer {_TWITTER_BEARER}",
                    "x-csrf-token": ct0,
                    "User-Agent": config.USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://x.com/",
                    "x-twitter-active-user": "yes",
                    "x-twitter-auth-type": "OAuth2Session",
                    "x-twitter-client-language": "en",
                },
            )
            if resp.ok:
                return await resp.json()
            logger.debug(
                "Twitter API %s → HTTP %d", url.rsplit("/", 1)[-1], resp.status
            )
            return None
        except Exception as exc:
            logger.debug("api_get failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # BrowserPool as async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BrowserPool":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()
