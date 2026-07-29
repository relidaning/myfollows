"""YouTube subscriptions fetcher — Playwright-driven, mirrors server.py's
Douyin flow but differs in one important way: Google actively blocks
automated credential entry, and there is no QR-scan login like Douyin's, so
login can't be fully headless/self-service. Instead:

- Login is a real interactive session: a *headed* Chromium is launched
  directly on the host's own X display (DISPLAY=:0, passed through via
  docker-compose's /tmp/.X11-unix mount — see docker-compose.yml), so a
  real Chromium window pops up on the desktop itself, same as any other
  app window. The user drives it themselves. Once they finish signing in
  (including any 2FA/passkey prompt), the session cookies are saved to
  youtube_storage_state.json and reused headlessly from then on, same as
  Douyin's storage_state.json. This only works because the container
  always runs on the same machine as the desktop it pops up on — a
  headless remote host would need a different approach (real OAuth,
  most likely).
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
        _headless_context = await _headless_browser.new_context(**kwargs)
        await _headless_context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    return _headless_context


async def _get_headed_context():
    """Fresh headed context on the host's real X display (DISPLAY=:0, see
    docker-compose.yml), for interactive login only — the window pops up
    directly on the desktop. Always starts clean (no storage_state) — a
    half-authed leftover session is more confusing than starting from a
    logged-out state."""
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


async def reload_session() -> dict:
    """Discards the cached headless context so the next call re-reads
    youtube_storage_state.json from disk. Needed after re-importing cookies
    (see scripts/import_youtube_cookies.py) — _get_headless_context only
    loads storage_state once per process lifetime otherwise (fresh=False
    reuses whatever context already exists), so without this an import
    would silently have no effect until the container restarts."""
    global _headless_context
    async with _headless_lock:
        if _headless_context is not None:
            await _headless_context.close()
            _headless_context = None
    return {"status": "reloaded"}


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
    """Open a real, interactive Chromium window directly on the host's
    desktop (see _get_headed_context) and point it at Google sign-in. This
    function only kicks off the page and returns immediately, it doesn't
    wait for the user — the caller polls login_poll()/login_wait()."""
    global _login_page
    async with _headed_lock:
        ctx = await _get_headed_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        _login_page = page
        return {"status": "window_opened"}


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

# The subscriptions feed's UI language follows the Google account's own
# language setting, not the Playwright context's `locale` — confirmed
# 2026-07-29 that an account set to Chinese renders "3天前" etc. regardless
# of context locale, so both patterns are needed.
_RELATIVE_RE_ZH = re.compile(r"(\d+)\s*(秒|分钟|小时|天|周|个月|年)前")
_UNIT_SECONDS_ZH = {
    "秒": 1, "分钟": 60, "小时": 3600, "天": 86400,
    "周": 604800, "个月": 2592000, "年": 31536000,
}


def _parse_relative_time(text: str) -> str:
    """'Streamed 3 hours ago' / '2 days ago' / '3天前' -> approximate ISO
    timestamp. YouTube's subscriptions feed only exposes relative text, not
    an exact timestamp, so this is inherently approximate — good enough for
    newest-first ordering, not for precise scheduling."""
    text = text or ""
    m = _RELATIVE_RE.search(text)
    if m:
        amount, seconds = int(m.group(1)), _UNIT_SECONDS.get(m.group(2).lower(), 0)
    else:
        m = _RELATIVE_RE_ZH.search(text)
        if not m:
            return ""
        amount, seconds = int(m.group(1)), _UNIT_SECONDS_ZH.get(m.group(2), 0)
    delta = datetime.timedelta(seconds=amount * seconds)
    return (datetime.datetime.now() - delta).isoformat(timespec="seconds")


async def _scroll_load_more(page, target_count: int) -> None:
    """Scrolls until at least `target_count` cards are loaded (or scrolling
    stalls/hits MAX_SCROLLS). Stopping at the target — not just on stall —
    matters a lot on accounts with many subscriptions: an unbounded scroll
    can pull in hundreds of cards (and their thumbnails) well past what the
    caller actually asked for, confirmed 2026-07-29 as the cause of a sync
    that took minutes and appeared hung."""
    stall = 0
    prev_count = -1
    for _ in range(MAX_SCROLLS):
        count = await page.locator(VIDEO_CARD_SELECTOR).count()
        if count >= target_count:
            break
        if count == prev_count:
            stall += 1
            if stall >= SCROLL_STALL_LIMIT:
                break
        else:
            stall = 0
        prev_count = count
        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        await page.wait_for_timeout(SCROLL_WAIT_MS)


async def _scrape_subscriptions(page, max_videos: int) -> list[dict]:
    cards = await page.locator(VIDEO_CARD_SELECTOR).all()
    videos = []
    for card in cards:
        if len(videos) >= max_videos:
            break
        # YouTube redesigned the feed around a `yt-lockup-view-model` card
        # (confirmed 2026-07-29) — the old `#video-title-link`/`#video-title`
        # ids are gone. Selectors below try the new markup first and fall
        # back to the old ids in case a card renders the legacy layout.
        title_el = card.locator(
            "h3.ytLockupMetadataViewModelHeadingReset a, #video-title-link, a#video-title"
        ).first
        if not await title_el.count():
            continue
        href = await title_el.get_attribute("href") or ""
        vid_match = re.search(r"[?&]v=([\w-]{6,})", href)
        if not vid_match:
            continue
        video_id = vid_match.group(1)

        # The heading's `title` attribute holds the full, untruncated text;
        # the link's own text/title can be truncated or include the
        # duration, so prefer the heading when present.
        heading_el = card.locator("h3.ytLockupMetadataViewModelHeadingReset, #video-title").first
        heading_title = await heading_el.get_attribute("title") if await heading_el.count() else None
        title = (heading_title or await title_el.get_attribute("title")
                 or (await title_el.inner_text()) or "").strip()

        channel_el = card.locator("a[href^='/@'], ytd-channel-name #text, ytd-channel-name a").first
        channel = (await channel_el.inner_text()).strip() if await channel_el.count() else ""

        # New layout splits metadata into two rows (channel, then
        # views + relative time); old layout has one flat `#metadata-line`.
        # Either way the relative-time text is the last span of the last row.
        meta_rows = card.locator(".ytContentMetadataViewModelMetadataRow, #metadata-line")
        published_text = ""
        rn = await meta_rows.count()
        if rn:
            meta_spans = meta_rows.nth(rn - 1).locator("span")
            n = await meta_spans.count()
            if n:
                published_text = (await meta_spans.nth(n - 1).inner_text()).strip()

        videos.append({
            "id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": title,
            "user": channel,
            "published_at": _parse_relative_time(published_text),
            "content": published_text,
            # Built from video_id via YouTube's own stable thumbnail CDN
            # instead of scraping the card's <img src> — confirmed
            # 2026-07-29 that grabbing "first img in the card" came back
            # empty/broken for a chunk of videos (lazy-loaded cards whose
            # <img> hadn't swapped in a real src yet by the time scraping
            # ran, or whose first <img> was a channel-avatar overlay, not
            # the thumbnail). i.ytimg.com/vi/{id}/hqdefault.jpg exists for
            # every valid video ID, no scraping/timing involved.
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
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

            target = max(limit, 1) * 3
            await _scroll_load_more(page, target)
            videos = await _scrape_subscriptions(page, target)
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


# Best-effort — UNVERIFIED against a live session at write time (2026-07-29).
# YouTube's DOM changes fairly often (see module docstring); if unsubscribing
# silently reports failure, inspect a real channel page with devtools and
# update these.
CHANNEL_LINK_SELECTOR = "ytd-video-owner-renderer a[href^='/@'], ytd-channel-name a[href^='/@']"
SUBSCRIBE_BUTTON_SELECTOR = "ytd-subscribe-button-renderer button, tp-yt-paper-button#subscribe-button"
UNSUBSCRIBE_CONFIRM_SELECTOR = (
    "yt-confirm-dialog-renderer #confirm-button button, "
    "tp-yt-paper-dialog button:has-text('Unsubscribe')"
)


async def unsubscribe(video_id: str) -> dict:
    """Unsubscribes from this video's channel — a real, hard-to-reverse
    action on the live Google account, not just a local DB change.

    We don't store a stable channel URL for YouTube creators (only a
    display name, same limitation as Douyin's follow sidebar — see
    _scrape_subscriptions), so this resolves the channel fresh from the
    video itself: opens the video's own watch page, follows its channel
    link, then clicks the real Subscribed button. Reports {"ok": false}
    honestly if the button still reads "Subscribed" afterward rather than
    assuming success from a click not erroring.
    """
    if not os.path.exists(common.YOUTUBE_STORAGE_STATE_PATH):
        return {"ok": False, "error": "Not logged in."}

    async with _headless_lock:
        ctx = await _get_headless_context(fresh=False)
        page = await ctx.new_page()
        try:
            await page.goto(
                f"https://www.youtube.com/watch?v={video_id}", wait_until="domcontentloaded", timeout=30000
            )
            channel_link = page.locator(CHANNEL_LINK_SELECTOR).first
            try:
                await channel_link.wait_for(state="visible", timeout=10000)
            except PlaywrightTimeoutError:
                return {
                    "ok": False,
                    "error": "Could not find this video's channel link — "
                    "YouTube's watch-page DOM may have changed.",
                }
            href = await channel_link.get_attribute("href")
            if not href:
                return {"ok": False, "error": "Channel link had no href."}
            channel_url = href if href.startswith("http") else f"https://www.youtube.com{href}"

            await page.goto(channel_url, wait_until="domcontentloaded", timeout=30000)
            sub_btn = page.locator(SUBSCRIBE_BUTTON_SELECTOR).first
            try:
                await sub_btn.wait_for(state="visible", timeout=10000)
            except PlaywrightTimeoutError:
                return {"ok": False, "error": "Could not find the subscribe button on the channel page."}

            btn_text = ((await sub_btn.inner_text()) or "").strip().lower()
            if "subscribed" not in btn_text:
                return {"ok": True}  # already not subscribed, or button text differs — nothing to undo

            await sub_btn.click()
            try:
                confirm_btn = page.locator(UNSUBSCRIBE_CONFIRM_SELECTOR).first
                await confirm_btn.wait_for(state="visible", timeout=3000)
                await confirm_btn.click()
            except PlaywrightTimeoutError:
                pass  # no confirmation dialog appeared — fine, not every account shows one

            await page.wait_for_timeout(1500)
            new_text = ((await sub_btn.inner_text()) or "").strip().lower()
            return {"ok": "subscribed" not in new_text}
        finally:
            await page.close()
