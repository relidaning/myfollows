"""YouTube subscriptions fetcher — Playwright-driven, mirrors server.py's
Douyin flow but differs in one important way: Google actively blocks
automated credential entry, and there is no QR-scan login like Douyin's, so
login can't be fully headless/self-service. Instead:

- Login is a real interactive session: a *headed* Chromium is launched on
  the container's virtual display (Xvfb, :99 — see start.sh), and the user
  drives it themselves through a noVNC window embedded in the web UI
  (http://localhost:6082/vnc.html). Once they finish signing in (including
  any 2FA/passkey prompt), the session cookies are saved to
  youtube_storage_state.json and reused headlessly from then on, same as
  Douyin's storage_state.json.
- Sync reads https://www.youtube.com/feed/subscriptions — YouTube's own
  "recent uploads from channels I'm subscribed to" feed, already sorted
  newest-first, so no per-channel enumeration is needed. Videos render as
  <ytd-rich-item-renderer> cards; if this stops matching (YouTube changes
  its DOM fairly often), inspect https://www.youtube.com/feed/subscriptions
  with devtools and update the selectors below.

Published timestamps come from YouTube's relative text ("3 hours ago", "2
days ago") — the DOM doesn't expose an exact timestamp here — so
published_at is an approximation, fine for "what's new" ordering/display
but not exact to the second.
"""

import asyncio
import datetime
import os
import re
from typing import Optional

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import common

YOUTUBE_HOME = "https://www.youtube.com/"
SUBSCRIPTIONS_URL = "https://www.youtube.com/feed/subscriptions"
LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fwww.youtube.com%2F"

LOGGED_IN_SELECTOR = "ytd-masthead #avatar-btn"
VIDEO_CARD_SELECTOR = "ytd-rich-item-renderer"
NOVNC_URL = "http://localhost:6082/vnc.html?autoconnect=true&resize=scale"

MAX_SCROLLS = 25
SCROLL_WAIT_MS = 900
SCROLL_STALL_LIMIT = 3

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Google's domains aren't reachable directly from this host — only through a
# local proxy (confirmed 2026-07-28: direct connections from the container
# time out even with network_mode: host, while curl through this proxy
# succeeds instantly; Douyin needs no such proxy, which is why the existing
# Douyin browser never set one). Chromium does NOT reliably pick up
# http_proxy/https_proxy from the environment on headless Linux — it needs
# an explicit `proxy` launch option, unlike curl/httpx/requests.
YOUTUBE_PROXY_SERVER = os.getenv("YOUTUBE_PROXY_SERVER", "http://127.0.0.1:10808")

# ---------------------------------------------------------------------------
# Browser lifecycle. Headless (sync) and headed (interactive login) browsers
# are kept separate — different launch-time flag, can't share one instance —
# each serialized through its own lock.
# ---------------------------------------------------------------------------

_pw = None
_headless_lock = asyncio.Lock()
_headless_browser = None
_headless_context = None

_headed_lock = asyncio.Lock()
_headed_browser = None
_headed_context = None
_login_page: Optional[Page] = None


async def _get_pw():
    global _pw
    if _pw is None:
        _pw = await async_playwright().start()
    return _pw


async def _get_headless_context(fresh: bool):
    global _headless_browser, _headless_context
    pw = await _get_pw()
    if _headless_browser is None:
        _headless_browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            proxy={"server": YOUTUBE_PROXY_SERVER} if YOUTUBE_PROXY_SERVER else None,
        )
    if fresh and _headless_context is not None:
        await _headless_context.close()
        _headless_context = None
    if _headless_context is None:
        kwargs = {
            "locale": "en-US",
            "viewport": {"width": 1280, "height": 900},
            "user_agent": _USER_AGENT,
        }
        if not fresh and os.path.exists(common.YOUTUBE_STORAGE_STATE_PATH):
            kwargs["storage_state"] = common.YOUTUBE_STORAGE_STATE_PATH
        _headless_context = await pw.chromium.new_context(**kwargs)
        await _headless_context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    return _headless_context


async def _get_headed_context():
    """Fresh headed context on the virtual display, for interactive login
    only. Always starts clean (no storage_state) — a half-authed leftover
    session is more confusing than starting from a logged-out state."""
    global _headed_browser, _headed_context
    pw = await _get_pw()
    if _headed_browser is not None:
        await _headed_browser.close()
        _headed_browser = None
        _headed_context = None
    _headed_browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--window-position=0,0"],
        proxy={"server": YOUTUBE_PROXY_SERVER} if YOUTUBE_PROXY_SERVER else None,
    )
    _headed_context = await _headed_browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=_USER_AGENT,
        locale="en-US",
    )
    return _headed_context


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def login_status() -> dict:
    if not os.path.exists(common.YOUTUBE_STORAGE_STATE_PATH):
        return {"logged_in": False, "reason": "no saved session"}
    async with _headless_lock:
        ctx = await _get_headless_context(fresh=False)
        page = await ctx.new_page()
        try:
            await page.goto(YOUTUBE_HOME, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=8000)
                return {"logged_in": True}
            except PlaywrightTimeoutError:
                return {"logged_in": False, "reason": "session expired or selector stale"}
        finally:
            await page.close()


async def login_start() -> dict:
    """Open a real, interactive Chromium on the virtual display and point it
    at Google sign-in. The caller (server.py's route) hands the user
    NOVNC_URL to interact with it directly — this function only kicks off
    the page and returns immediately, it doesn't wait for the user."""
    global _login_page
    async with _headed_lock:
        ctx = await _get_headed_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        _login_page = page
        return {"status": "vnc_ready", "vnc_url": NOVNC_URL}


async def login_poll() -> dict:
    """Cheap, non-blocking check for UI polling — never blocks, unlike
    login_wait below."""
    global _login_page, _headed_context
    async with _headed_lock:
        if _login_page is None:
            return {"logged_in": os.path.exists(common.YOUTUBE_STORAGE_STATE_PATH), "in_progress": False}
        page = _login_page
        try:
            found = await page.locator(LOGGED_IN_SELECTOR).count() > 0
        except Exception:
            found = False
        if found:
            await page.context.storage_state(path=common.YOUTUBE_STORAGE_STATE_PATH)
            await _headed_context.close()
            _headed_context = None
            _login_page = None
            return {"logged_in": True, "in_progress": False}
        return {"logged_in": False, "in_progress": True}


async def login_wait(timeout_sec: int) -> dict:
    global _login_page, _headed_context
    async with _headed_lock:
        if _login_page is None:
            return {"status": "error", "message": "Call youtube login_start first."}
        page = _login_page
        try:
            await page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=timeout_sec * 1000)
        except PlaywrightTimeoutError:
            return {"status": "timeout", "message": "Login was not completed in time."}

        await page.context.storage_state(path=common.YOUTUBE_STORAGE_STATE_PATH)
        await _headed_context.close()
        _headed_context = None
        _login_page = None
        return {"status": "success"}


# ---------------------------------------------------------------------------
# Subscriptions sync
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)
_UNIT_SECONDS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 604800, "month": 2592000, "year": 31536000,
}


def _parse_relative_time(text: str) -> str:
    """'Streamed 3 hours ago' / '2 days ago' -> approximate ISO timestamp.
    YouTube's subscriptions feed only exposes relative text, not an exact
    timestamp, so this is inherently approximate — good enough for
    newest-first ordering, not for precise scheduling."""
    m = _RELATIVE_RE.search(text or "")
    if not m:
        return ""
    amount, unit = int(m.group(1)), m.group(2).lower()
    delta = datetime.timedelta(seconds=amount * _UNIT_SECONDS.get(unit, 0))
    return (datetime.datetime.now() - delta).isoformat(timespec="seconds")


async def _scroll_load_more(page) -> None:
    stall = 0
    prev_count = -1
    for _ in range(MAX_SCROLLS):
        count = await page.locator(VIDEO_CARD_SELECTOR).count()
        if count == prev_count:
            stall += 1
            if stall >= SCROLL_STALL_LIMIT:
                break
        else:
            stall = 0
        prev_count = count
        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        await page.wait_for_timeout(SCROLL_WAIT_MS)


async def _scrape_subscriptions(page) -> list[dict]:
    cards = await page.locator(VIDEO_CARD_SELECTOR).all()
    videos = []
    for card in cards:
        title_el = card.locator("#video-title-link, a#video-title").first
        if not await title_el.count():
            continue
        href = await title_el.get_attribute("href") or ""
        vid_match = re.search(r"[?&]v=([\w-]{6,})", href)
        if not vid_match:
            continue
        video_id = vid_match.group(1)
        title = (await title_el.get_attribute("title") or (await title_el.inner_text()) or "").strip()

        channel_el = card.locator("ytd-channel-name #text, ytd-channel-name a").first
        channel = (await channel_el.inner_text()).strip() if await channel_el.count() else ""

        meta_spans = card.locator("#metadata-line span")
        published_text = ""
        n = await meta_spans.count()
        if n:
            published_text = (await meta_spans.nth(n - 1).inner_text()).strip()

        thumb_el = card.locator("ytd-thumbnail img").first
        thumb = await thumb_el.get_attribute("src") if await thumb_el.count() else ""

        videos.append({
            "id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": title,
            "user": channel,
            "published_at": _parse_relative_time(published_text),
            "content": published_text,
            "thumbnail_url": thumb or "",
            "play_url": "",
        })
    return videos


async def sync(limit: int = 30) -> dict:
    if not os.path.exists(common.YOUTUBE_STORAGE_STATE_PATH):
        return {"error": "Not logged in."}

    async with _headless_lock:
        ctx = await _get_headless_context(fresh=False)
        page = await ctx.new_page()
        try:
            await page.goto(SUBSCRIPTIONS_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=15000)
            except PlaywrightTimeoutError:
                logged_in = await page.locator(LOGGED_IN_SELECTOR).count() > 0
                if not logged_in:
                    return {"error": "Session looks logged out — log in again."}
                return {
                    "error": "No subscription videos found. YouTube's subscriptions-feed "
                    "DOM may have changed — inspect the page with devtools and update "
                    "VIDEO_CARD_SELECTOR / _scrape_subscriptions in youtube.py.",
                }

            await _scroll_load_more(page)
            videos = (await _scrape_subscriptions(page))[: max(limit, 1) * 3]
        finally:
            await page.close()

    creators = []
    seen = set()
    for v in videos:
        if v["user"] and v["user"] not in seen:
            seen.add(v["user"])
            creators.append({"name": v["user"], "avatar_url": "", "unread_count": 0})

    common.upsert_creators(creators, platform="youtube")
    new_count, total = common.upsert_videos(videos, platform="youtube")
    return {
        "fetched": len(videos),
        "new": new_count,
        "total_in_db": total,
        "creators": len(creators),
    }
