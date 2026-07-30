"""douyin-mcp — Playwright-driven Douyin follow-feed fetcher + browsable UI.

MCP server that drives a headless Chromium session (via Playwright) against
douyin.com, persists the logged-in session to disk so login only happens
once, and syncs videos published by followed creators into a local SQLite
store. A small web UI (served on the same port) lets the user browse
thumbnails and mark videos watched — this is the primary way to consume the
feed; the MCP tools exist mainly to bootstrap login and trigger a sync from
a Claude session.

Douyin's DOM/selectors and internal APIs are not publicly documented and
change without notice. Login uses best-effort DOM selectors (see
QR_SELECTOR/LOGGED_IN_SELECTOR below); the follow feed instead intercepts
the real `/aweme/v1/web/follow/feed/` JSON API the page itself calls, which
is far more stable than scraping its single-video story-style player. If
either breaks, inspect https://www.douyin.com/follow with devtools/network
tab and update the relevant constant or `_parse_feed_response` below.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import httpx
from fastmcp import FastMCP
from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

import common
import youtube
from common import LABEL_CATEGORIES, QR_IMAGE_PATH, _VIDEO_COLUMNS, _classify_labels, _db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    force=True,
)
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STORAGE_STATE_PATH = common.DOUYIN_STORAGE_STATE_PATH
UI_HTML_PATH = os.path.join(os.path.dirname(__file__), "ui.html")

DOUYIN_HOME = "https://www.douyin.com/"
FOLLOW_FEED_URL = "https://www.douyin.com/follow"

# Best-effort selectors — see module docstring. Confirmed 2026-07-28: the QR
# is not a plain <img>/<canvas> — Douyin renders it as a Lottie-animated SVG
# (base64 webp frames) nested inside the stable id #default_scan_code_guide,
# itself inside #douyin_login_comp_scan_code. The broader fallbacks below are
# kept in case Douyin swaps the rendering approach again.
QR_SELECTOR = (
    "#default_scan_code_guide, #animate_qrcode_container, "
    "#douyin_login_comp_scan_code, img[alt*='二维码'], "
    "[class*='qrcode'] img, [class*='qrcode'] canvas"
)
LOGGED_IN_SELECTOR = "[class*='avatar'], [data-e2e='user-avatar']"
NEXT_ARROW_SELECTOR = "[data-e2e='video-switch-next-arrow']"
SAVE_LOGIN_DIALOG_DISMISS_TEXT = "取消"  # "Cancel" on the post-login "save login info?" prompt
MAX_NEXT_CLICKS = 40
NEXT_CLICK_WAIT_MS = 1800

mcp = FastMCP("myfollows")

# ---------------------------------------------------------------------------
# Browser lifecycle — single shared instance for the life of the process.
# Playwright objects aren't safe for concurrent use, so all tools serialize
# through _lock.
# ---------------------------------------------------------------------------

_lock = asyncio.Lock()
_playwright = None
_browser = None
_context = None
_login_page: Optional[Page] = None


async def _get_context(fresh: bool):
    """Return the shared browser context, (re)creating it if needed.

    fresh=True discards any existing context and starts an anonymous one
    (used to kick off a new login). fresh=False reuses the persisted
    storage_state (cookies) from a prior successful login.
    """
    global _playwright, _browser, _context

    if _playwright is None:
        _playwright = await async_playwright().start()
    if _browser is None:
        # --disable-blink-features=AutomationControlled trims the most
        # obvious automation fingerprint. This does NOT fix Douyin serving
        # a 验证码中间页 (CAPTCHA interstitial) on the very first request —
        # that's server-side IP/risk-control, decided before any JS runs,
        # and no client-side flag can work around it. See SKILL.md.
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

    if fresh and _context is not None:
        await _context.close()
        _context = None

    if _context is None:
        kwargs = {
            "locale": "zh-CN",
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        if not fresh and os.path.exists(STORAGE_STATE_PATH):
            kwargs["storage_state"] = STORAGE_STATE_PATH
        _context = await _browser.new_context(**kwargs)
        await _context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    return _context


# SQLite store, upsert helpers, and _VIDEO_COLUMNS now live in common.py
# (shared with youtube.py) — imported at module top.

# ---------------------------------------------------------------------------
# Login (shared impl used by both MCP tools and REST routes)
# ---------------------------------------------------------------------------


async def _login_status_impl() -> dict:
    if not os.path.exists(STORAGE_STATE_PATH):
        return {"logged_in": False, "reason": "no saved session"}
    async with _lock:
        ctx = await _get_context(fresh=False)
        page = await ctx.new_page()
        try:
            await page.goto(DOUYIN_HOME, wait_until="domcontentloaded", timeout=20000)
            try:
                await page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=8000)
                return {"logged_in": True}
            except PlaywrightTimeoutError:
                return {"logged_in": False, "reason": "session expired or selector stale"}
        finally:
            await page.close()


async def _login_start_impl() -> dict:
    global _login_page
    async with _lock:
        ctx = await _get_context(fresh=True)
        page = await ctx.new_page()
        await page.goto(DOUYIN_HOME, wait_until="domcontentloaded", timeout=30000)

        title = await page.title()
        if "验证码" in title or "中间页" in title:
            await page.close()
            return {
                "status": "error",
                "reason": "captcha_wall",
                "message": (
                    f"Douyin served a CAPTCHA interstitial (page title: {title!r}) instead of "
                    "the app. This is server-side IP/fingerprint risk-control decided before any "
                    "page JS runs — it is not a stale selector, and no client-side flag fixes it. "
                    "Retry later, or run this container from a network Douyin treats as trusted "
                    "(e.g. the same machine/network you normally browse Douyin from)."
                ),
            }

        try:
            await page.wait_for_selector(QR_SELECTOR, timeout=15000)
        except PlaywrightTimeoutError:
            login_trigger = page.get_by_text("登录", exact=False).first
            if await login_trigger.count():
                await login_trigger.click()
                await page.wait_for_selector(QR_SELECTOR, timeout=15000)
            else:
                await page.close()
                return {
                    "status": "error",
                    "reason": "stale_selector",
                    "message": "Could not find the QR login element. Douyin's login markup "
                    "may have changed — inspect the page and update QR_SELECTOR in server.py.",
                }

        # The QR is a Lottie animation that opens on a loading frame before
        # settling on the actual scannable pattern — wait_for_selector only
        # confirms the element exists, not that painting is done.
        qr_el = page.locator(QR_SELECTOR).first
        await page.wait_for_timeout(2500)
        await qr_el.screenshot(path=QR_IMAGE_PATH)
        _login_page = page
        return {"status": "qr_ready", "qr_image_path": QR_IMAGE_PATH}


async def _login_wait_impl(timeout_sec: int) -> dict:
    global _login_page
    async with _lock:
        if _login_page is None:
            return {"status": "error", "message": "Call douyin_login_start first."}
        page = _login_page
        try:
            await page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=timeout_sec * 1000)
        except PlaywrightTimeoutError:
            return {"status": "timeout", "message": "QR code was not scanned in time. Call douyin_login_start again for a fresh code."}

        await page.context.storage_state(path=STORAGE_STATE_PATH)
        await page.close()
        _login_page = None
        return {"status": "success"}


async def _login_poll_impl() -> dict:
    """Single, cheap check against the already-open login page (if any) —
    for UI polling, unlike _login_wait_impl this never blocks."""
    global _login_page
    async with _lock:
        if _login_page is None:
            return {"logged_in": os.path.exists(STORAGE_STATE_PATH), "in_progress": False}
        page = _login_page
        try:
            found = await page.locator(LOGGED_IN_SELECTOR).count() > 0
        except Exception:
            found = False
        if found:
            await page.context.storage_state(path=STORAGE_STATE_PATH)
            await page.close()
            _login_page = None
            return {"logged_in": True, "in_progress": False}
        return {"logged_in": False, "in_progress": True}


@mcp.tool()
async def douyin_login_status() -> dict:
    """Check whether a saved Douyin session exists and is still valid."""
    return await _login_status_impl()


@mcp.tool()
async def douyin_login_start() -> dict:
    """Open douyin.com and save a screenshot of the login QR code.

    Send the image at the returned path to the user (e.g. via SendUserFile,
    or open it directly if this host has a display) so they can scan it
    with the Douyin app. Follow up with douyin_login_wait() once the user
    confirms they scanned it. Prefer telling the user to just open the UI
    at http://localhost:8082/ instead — it handles the QR display and
    polling itself.
    """
    return await _login_start_impl()


@mcp.tool()
async def douyin_login_wait(timeout_sec: int = 90) -> dict:
    """Wait for the user to finish scanning the QR code, then persist the session."""
    return await _login_wait_impl(timeout_sec)


# ---------------------------------------------------------------------------
# Feed sync (shared impl)
# ---------------------------------------------------------------------------
#
# Confirmed 2026-07-28: the /follow page is a single-video story-style
# player (left sidebar of creators, one video at a time on the right), not a
# scrollable card grid — DOM scraping doesn't apply here. But the page loads
# its data from a real JSON API, `/aweme/v1/web/follow/feed/`, fired
# automatically on page load and again each time the "next video" arrow is
# clicked (client-side pagination via a cursor, no URL change). We intercept
# those responses instead of scraping the DOM: it's the actual data source,
# gives an exact `create_time` unix timestamp and a real shareable
# `share_url` per video. Clicking "next" repeatedly is what drives the
# pagination forward — a single page load only returns the first batch.


def _parse_feed_response(body: dict, out: dict) -> None:
    """Extract {aweme_id: video dict} from one follow/feed/ JSON response into out."""
    for entry in body.get("data", []) or []:
        aweme = entry.get("aweme")
        if not aweme or not aweme.get("aweme_id"):
            continue
        aweme_id = aweme["aweme_id"]
        if aweme_id in out:
            continue
        desc = (aweme.get("desc") or "").strip()
        share_url = (aweme.get("share_url") or "").split("?")[0]
        create_time = aweme.get("create_time")
        video_obj = aweme.get("video") or {}
        cover_list = (video_obj.get("cover") or {}).get("url_list") or []
        # play_addr_h264 is preferred for browser compatibility; play_addr
        # can be h265/bytevc1 depending on source, which not all browsers
        # decode. Both require a Referer: douyin.com header to fetch — see
        # /api/play/{id} proxy route, direct hotlinking 403s unlike covers.
        play_list = (video_obj.get("play_addr_h264") or video_obj.get("play_addr") or {}).get("url_list") or []
        out[aweme_id] = {
            "id": aweme_id,
            "url": share_url or f"https://www.douyin.com/video/{aweme_id}",
            "title": desc,
            "user": ((aweme.get("author") or {}).get("nickname") or "").strip(),
            "published_at": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(create_time))
                if create_time
                else ""
            ),
            "content": desc,
            "thumbnail_url": cover_list[0] if cover_list else "",
            "play_url": play_list[0] if play_list else "",
        }


CREATOR_LIST_SELECTOR = "ul.VifoXYqW li"


CREATOR_LIST_SCROLL_CONTAINER = "ul.VifoXYqW"  # .closest('.MkTWFeTR') is the actual scroller
CREATOR_LIST_MAX_SCROLLS = 30
CREATOR_LIST_SCROLL_WAIT_MS = 800
CREATOR_LIST_STALL_LIMIT = 3  # consecutive no-growth reads before giving up


async def _scroll_load_all_creators(page) -> None:
    """The sidebar only renders ~40 <li> at a time and lazily appends more
    as its container is scrolled (confirmed 2026-07-28: with 126 followed
    creators, only 40 were in the DOM until scrolled — it's append-only, not
    a windowed/virtualized list, so previously-rendered items stay put).

    Two things that look like reasonable first attempts don't work here:
    - Scrolling by a small fixed increment (e.g. +600px) doesn't reliably
      cross whatever internal threshold triggers the next batch to load —
      jump straight to `scrollHeight` (bottom) each time instead.
    - Breaking on the first no-growth read is too eager: the count can stay
      flat for one poll right after a scroll before the next batch renders.
      Requires CREATOR_LIST_STALL_LIMIT consecutive flat reads, not one.
    """
    stall = 0
    prev_count = -1
    for _ in range(CREATOR_LIST_MAX_SCROLLS):
        count = await page.locator(CREATOR_LIST_SELECTOR).count()
        if count == prev_count:
            stall += 1
            if stall >= CREATOR_LIST_STALL_LIMIT:
                break
        else:
            stall = 0
        prev_count = count
        try:
            await page.evaluate(
                """(sel) => {
                    const ul = document.querySelector(sel);
                    const scroller = ul && ul.closest('.MkTWFeTR');
                    if (scroller) scroller.scrollTop = scroller.scrollHeight;
                }""",
                CREATOR_LIST_SCROLL_CONTAINER,
            )
        except Exception:
            break
        await page.wait_for_timeout(CREATOR_LIST_SCROLL_WAIT_MS)


async def _scrape_creators(page) -> list[dict]:
    """Scrape the followed-creator sidebar on /follow: name, avatar, unread count.

    Confirmed 2026-07-28: each <li> has no href/user-id in its markup (it's
    a JS click handler, not a link) — name is used as the natural key. The
    unread badge ("N个作品未看") is a second line of text inside the same
    name div when present, absent otherwise.
    """
    await _scroll_load_all_creators(page)
    creators = []
    items = await page.locator(CREATOR_LIST_SELECTOR).all()
    for it in items:
        name_el = it.locator(".lEYk5unc").first
        if not await name_el.count():
            continue
        text = (await name_el.inner_text()).strip()
        if not text:
            continue
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        name = lines[0]
        unread = 0
        for line in lines[1:]:
            if "未看" in line:
                digits = "".join(ch for ch in line if ch.isdigit())
                unread = int(digits) if digits else 0
        avatar_el = it.locator("img").first
        avatar_src = await avatar_el.get_attribute("src") if await avatar_el.count() else ""
        if avatar_src and avatar_src.startswith("//"):
            avatar_src = "https:" + avatar_src
        creators.append({"name": name, "avatar_url": avatar_src or "", "unread_count": unread})
    return creators


async def _run_sync(limit: int) -> dict:
    if not os.path.exists(STORAGE_STATE_PATH):
        return {"error": "Not logged in."}

    async with _lock:
        ctx = await _get_context(fresh=False)
        page = await ctx.new_page()
        items: dict[str, dict] = {}

        async def on_response(resp):
            if "/aweme/v1/web/follow/feed/" in resp.url:
                try:
                    body = await resp.json()
                except Exception:
                    return
                _parse_feed_response(body, items)

        page.on("response", on_response)

        try:
            await page.goto(FOLLOW_FEED_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)  # let the initial feed/ call land

            logged_in = await page.locator(LOGGED_IN_SELECTOR).count() > 0
            if not logged_in:
                return {"error": "Session looks logged out — log in again."}

            dismiss = page.get_by_text(SAVE_LOGIN_DIALOG_DISMISS_TEXT, exact=True).first
            if await dismiss.count():
                await dismiss.click()
                await page.wait_for_timeout(500)

            creators = await _scrape_creators(page)

            if not items:
                return {
                    "error": "No feed items found. Douyin's follow-feed API "
                    "(/aweme/v1/web/follow/feed/) may have changed shape — "
                    "inspect a live response and update _parse_feed_response in server.py.",
                }

            next_btn = page.locator(NEXT_ARROW_SELECTOR).first
            clicks = 0
            max_clicks = min(limit, MAX_NEXT_CLICKS)
            while clicks < max_clicks:
                if await next_btn.count() == 0:
                    break
                classes = await next_btn.get_attribute("class") or ""
                if "disabled" in classes:
                    break
                await next_btn.click()
                clicks += 1
                await page.wait_for_timeout(NEXT_CLICK_WAIT_MS)
        finally:
            await page.close()

    common.upsert_creators(creators, platform="douyin")
    new_count, total = common.upsert_videos(list(items.values()), platform="douyin")
    return {
        "fetched": len(items),
        "new": new_count,
        "total_in_db": total,
        "creators": len(creators),
    }


async def _refetch_play_url(page, video_id: str) -> bool:
    """Visits one video's own page (douyin.com/video/{id}), which triggers
    Douyin's aweme/detail API with full video data including play_addr, and
    writes it straight to that row — always overwrites, regardless of the
    row's current play_url. Shared by both _backfill_play_urls (rows that
    never got one) and _refresh_play_url (a row whose play_url is populated
    but its signed CDN token has since expired)."""
    detail = {}

    async def on_response(resp, _store=detail):
        if "aweme/detail" in resp.url:
            try:
                _store["body"] = await resp.json()
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(
            f"https://www.douyin.com/video/{video_id}",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await page.wait_for_timeout(2500)
    except PlaywrightTimeoutError:
        pass
    finally:
        page.remove_listener("response", on_response)

    body = detail.get("body")
    aweme = (body or {}).get("aweme_detail") if body else None
    if not aweme:
        return False
    video_obj = aweme.get("video") or {}
    play_list = (video_obj.get("play_addr_h264") or video_obj.get("play_addr") or {}).get("url_list") or []
    cover_list = (video_obj.get("cover") or {}).get("url_list") or []
    if not play_list:
        return False
    conn = _db()
    conn.execute(
        "UPDATE videos SET play_url = ?, "
        "thumbnail_url = COALESCE(NULLIF(thumbnail_url, ''), ?) "
        "WHERE id = ?",
        (play_list[0], cover_list[0] if cover_list else "", video_id),
    )
    conn.commit()
    conn.close()
    return True


async def _backfill_play_urls(limit: int = 50) -> dict:
    """Fill in play_url for rows synced before that field existed.

    Confirmed 2026-07-28: `_run_sync`'s INSERT...ON CONFLICT backfill only
    fires opportunistically if a video happens to resurface in a future
    (non-deterministic) feed sync — most old rows never get one. This
    instead visits each missing video's own page directly via
    _refetch_play_url. Slower than a feed sync (one page load per video,
    ~2-3s each) — capped by `limit` per call, safe to call repeatedly until
    none remain.
    """
    if not os.path.exists(STORAGE_STATE_PATH):
        return {"error": "Not logged in."}

    conn = _db()
    rows = conn.execute(
        "SELECT id FROM videos WHERE play_url IS NULL OR play_url = '' LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    if not rows:
        return {"attempted": 0, "updated": 0, "remaining": 0}

    updated = 0
    async with _lock:
        ctx = await _get_context(fresh=False)
        page = await ctx.new_page()
        try:
            for (video_id,) in rows:
                if await _refetch_play_url(page, video_id):
                    updated += 1
        finally:
            await page.close()

    conn = _db()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE play_url IS NULL OR play_url = ''"
    ).fetchone()[0]
    conn.close()
    return {"attempted": len(rows), "updated": updated, "remaining": remaining}


async def _refresh_play_url(video_id: str) -> dict:
    """Re-fetches a single video's play_url regardless of its current
    value — unlike _backfill_play_urls (which only targets rows that never
    got one), this is for a video whose signed CDN URL has since expired.

    Confirmed 2026-07-29: Douyin's play_addr URLs carry a short-lived signed
    token (~1 day, a `dy_q` unix-timestamp query param) — /api/play/{id}
    403s once that passes even though play_url is populated, which is a
    different failure mode than "never had a play_url" but the player's
    generic error handler couldn't previously tell them apart.
    """
    if not os.path.exists(STORAGE_STATE_PATH):
        return {"error": "Not logged in."}
    async with _lock:
        ctx = await _get_context(fresh=False)
        page = await ctx.new_page()
        try:
            ok = await _refetch_play_url(page, video_id)
        finally:
            await page.close()
    return {"ok": ok}


# Best-effort — the follow button's text ("已关注") is used instead of a CSS
# class since Douyin's class names are build-hashed and unstable across
# deploys (see QR_SELECTOR's docstring for the same caveat); text content is
# more likely to survive a redesign, though still not guaranteed. UNVERIFIED
# against a live session at write time (2026-07-29) — if unfollowing
# silently reports failure, inspect a real followed creator's profile page
# with devtools and update these.
DOUYIN_FOLLOWED_BUTTON_SELECTOR = "button:has-text('已关注')"
DOUYIN_UNFOLLOW_CONFIRM_SELECTOR = "button:has-text('确定'), button:has-text('确认')"


async def _unfollow_douyin_creator(video_id: str) -> dict:
    """Unfollows this video's creator on Douyin — a real, hard-to-reverse
    action on the live account, not just a local DB change.

    We don't store a stable profile id for Douyin creators (the follow
    sidebar in _scrape_creators only ever captures a display name, no
    href/user-id — see its docstring), so this resolves the creator fresh
    from the video itself: visits the video's own page to intercept the
    aweme/detail response (same technique as _refetch_play_url) and reads
    author.sec_uid out of it, then navigates to that profile and clicks the
    real unfollow button. Reports {"ok": false} honestly (rather than
    assuming success from a click not erroring) if the button never
    disappears afterward.
    """
    if not os.path.exists(STORAGE_STATE_PATH):
        return {"ok": False, "error": "Not logged in."}

    async with _lock:
        ctx = await _get_context(fresh=False)
        page = await ctx.new_page()
        try:
            detail = {}

            async def on_response(resp, _store=detail):
                if "aweme/detail" in resp.url:
                    try:
                        _store["body"] = await resp.json()
                    except Exception:
                        pass

            page.on("response", on_response)
            try:
                await page.goto(
                    f"https://www.douyin.com/video/{video_id}",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await page.wait_for_timeout(2500)
            except PlaywrightTimeoutError:
                pass
            finally:
                page.remove_listener("response", on_response)

            body = detail.get("body")
            aweme = (body or {}).get("aweme_detail") if body else None
            sec_uid = ((aweme or {}).get("author") or {}).get("sec_uid")
            if not sec_uid:
                return {"ok": False, "error": "Could not resolve the creator's profile from this video."}

            await page.goto(
                f"https://www.douyin.com/user/{sec_uid}", wait_until="domcontentloaded", timeout=20000
            )
            await page.wait_for_timeout(2000)

            follow_btn = page.locator(DOUYIN_FOLLOWED_BUTTON_SELECTOR).first
            try:
                await follow_btn.wait_for(state="visible", timeout=8000)
            except PlaywrightTimeoutError:
                return {
                    "ok": False,
                    "error": "Could not find a 'following' button on the creator's profile — "
                    "you may already not be following them, or Douyin's page layout changed.",
                }

            await follow_btn.click()
            try:
                confirm_btn = page.locator(DOUYIN_UNFOLLOW_CONFIRM_SELECTOR).first
                await confirm_btn.wait_for(state="visible", timeout=3000)
                await confirm_btn.click()
            except PlaywrightTimeoutError:
                pass  # no confirmation dialog appeared — fine, not every unfollow shows one

            await page.wait_for_timeout(1500)
            still_following = await page.locator(DOUYIN_FOLLOWED_BUTTON_SELECTOR).count()
            return {"ok": still_following == 0}
        finally:
            await page.close()


@mcp.tool()
async def douyin_sync_feed(limit: int = 30) -> dict:
    """Fetch recent videos from followed creators into the local video database.

    Returns a summary ({fetched, new, total_in_db}), not the full video
    list — browsing and marking watched happens in the UI at
    http://localhost:8082/, not through Claude. Videos about drawing/
    painting or gym/workout are auto-tagged and hidden from the UI's default
    view (see FILTER_CATEGORIES in server.py).
    """
    return await _run_sync(limit)


@mcp.tool()
async def douyin_backfill_play_urls(limit: int = 50) -> dict:
    """Fetch playable video links for older rows synced before that field existed.

    One page load per video (~2-3s each) — call repeatedly (it's capped by
    `limit`) until `remaining` is 0. Videos without a play_url show a
    fallback message in the UI instead of playing inline.
    """
    return await _backfill_play_urls(limit)


# ---------------------------------------------------------------------------
# YouTube MCP tools — thin wrappers around youtube.py. Login differs from
# Douyin's: there's no QR flow, so youtube_login_start opens a real,
# interactive Chromium window directly on the host's own desktop (see
# youtube.py's module docstring) and the user logs in themselves in that
# window. Prefer telling the user to use the UI at http://localhost:8082/
# rather than driving this from chat — there's a live browser window to
# interact with, which chat can't do.
# ---------------------------------------------------------------------------


@mcp.tool()
async def youtube_login_status() -> dict:
    """Check whether a saved YouTube/Google session exists and is still valid."""
    return await youtube.login_status()


@mcp.tool()
async def youtube_login_start() -> dict:
    """Open an interactive Chromium window at Google sign-in, directly on
    the host's own desktop.

    Returns {"status": "window_opened"} — tell the user a real browser
    window just opened on their desktop and to log in there themselves;
    automated credential entry is not attempted since Google blocks it.
    Follow up with youtube_login_wait() once they say they're done.
    """
    return await youtube.login_start()


@mcp.tool()
async def youtube_login_wait(timeout_sec: int = 180) -> dict:
    """Wait for the user to finish the interactive Google login, then persist the session."""
    return await youtube.login_wait(timeout_sec)


@mcp.tool()
async def youtube_sync_feed(limit: int = 30) -> dict:
    """Fetch recently published videos from YouTube subscriptions into the local video database.

    Reads https://www.youtube.com/feed/subscriptions (already newest-first).
    Returns a summary ({fetched, new, total_in_db}), not the full video list
    — browsing happens in the UI at http://localhost:8082/.
    """
    return await youtube.sync(limit)


# ---------------------------------------------------------------------------
# REST API + UI (same port as the MCP endpoint, via custom_route)
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
async def ui_index(request: Request) -> Response:
    # No cache headers were being sent at all, which let some browsers serve
    # a stale copy of ui.html across edits during active development —
    # confirmed 2026-07-28 (user kept seeing old player sizing after fixes
    # that tested correctly server-side). Force a fresh fetch every time.
    return HTMLResponse(
        open(UI_HTML_PATH, encoding="utf-8").read(),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@mcp.custom_route("/api/status", methods=["GET"])
async def api_status(request: Request) -> Response:
    return JSONResponse(await _login_poll_impl())


@mcp.custom_route("/api/login/start", methods=["POST"])
async def api_login_start(request: Request) -> Response:
    return JSONResponse(await _login_start_impl())


@mcp.custom_route("/api/login/qr.png", methods=["GET"])
async def api_login_qr(request: Request) -> Response:
    if not os.path.exists(QR_IMAGE_PATH):
        return JSONResponse({"error": "no QR available"}, status_code=404)
    return FileResponse(QR_IMAGE_PATH)


@mcp.custom_route("/api/sync", methods=["POST"])
async def api_sync(request: Request) -> Response:
    limit = int(request.query_params.get("limit", "30"))
    return JSONResponse(await _run_sync(limit))


@mcp.custom_route("/api/backfill", methods=["POST"])
async def api_backfill(request: Request) -> Response:
    limit = int(request.query_params.get("limit", "50"))
    return JSONResponse(await _backfill_play_urls(limit))


@mcp.custom_route("/api/videos/{video_id}/refresh_play_url", methods=["POST"])
async def api_refresh_play_url(request: Request) -> Response:
    """Re-fetches this one video's play_url even if it's already populated —
    for when playback 403s because the previously-saved signed CDN URL
    expired, not because play_url was never captured (see
    _refresh_play_url's docstring)."""
    video_id = request.path_params["video_id"]
    return JSONResponse(await _refresh_play_url(video_id))


@mcp.custom_route("/api/youtube/status", methods=["GET"])
async def api_youtube_status(request: Request) -> Response:
    return JSONResponse(await youtube.login_poll())


@mcp.custom_route("/api/youtube/login/start", methods=["POST"])
async def api_youtube_login_start(request: Request) -> Response:
    return JSONResponse(await youtube.login_start())


@mcp.custom_route("/api/youtube/reload_session", methods=["POST"])
async def api_youtube_reload_session(request: Request) -> Response:
    """Called by scripts/import_youtube_cookies.py after writing a fresh
    youtube_storage_state.json, so the import takes effect immediately
    instead of needing a container restart."""
    return JSONResponse(await youtube.reload_session())


@mcp.custom_route("/api/youtube/sync", methods=["POST"])
async def api_youtube_sync(request: Request) -> Response:
    limit = int(request.query_params.get("limit", "30"))
    return JSONResponse(await youtube.sync(limit))


@mcp.custom_route("/api/videos", methods=["GET"])
async def api_videos(request: Request) -> Response:
    watched_param = request.query_params.get("watched")
    show_filtered = request.query_params.get("show_filtered") == "true"
    user_param = request.query_params.get("user")
    platform_param = request.query_params.get("platform")
    label_param = request.query_params.get("label")
    starred_param = request.query_params.get("starred")

    query = f"SELECT {', '.join(_VIDEO_COLUMNS)} FROM videos"
    conditions = []
    params: list = []
    if watched_param is not None:
        conditions.append("watched = ?")
        params.append(1 if watched_param == "true" else 0)
    if not show_filtered:
        conditions.append("filtered_category IS NULL")
    if user_param:
        conditions.append("user = ?")
        params.append(user_param)
    if platform_param:
        conditions.append("platform = ?")
        params.append(platform_param)
    if starred_param == "true":
        conditions.append("starred = 1")
    if label_param:
        # labels is a comma-joined string (e.g. "ai_tech,programming") — match
        # label_param as one of the comma-separated entries, not a substring
        # of a different label name.
        conditions.append("(',' || labels || ',') LIKE ?")
        params.append(f"%,{label_param},%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY published_at DESC"

    conn = _db()
    rows = conn.execute(query, params).fetchall()
    users = [r[0] for r in conn.execute("SELECT DISTINCT user FROM videos ORDER BY user").fetchall()]
    label_groups = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT labels FROM videos WHERE labels IS NOT NULL AND labels != ''"
        ).fetchall()
    ]
    conn.close()

    videos = [dict(zip(_VIDEO_COLUMNS, row)) for row in rows]
    for v in videos:
        v["watched"] = bool(v["watched"])
        v["starred"] = bool(v["starred"])
        v["labels"] = v["labels"].split(",") if v["labels"] else []

    # Custom labels (added ad hoc via the "+" chip, not in LABEL_CATEGORIES)
    # are promoted here so they become a selectable chip on every video and
    # a real option in the top-bar filter dropdown, not just an active chip
    # on the one video they were first typed on.
    custom_labels = sorted({
        label
        for group in label_groups
        for label in group.split(",")
        if label not in LABEL_CATEGORIES
    })
    label_categories = list(LABEL_CATEGORIES.keys()) + custom_labels
    return JSONResponse({"videos": videos, "users": users, "label_categories": label_categories})


@mcp.custom_route("/api/videos/{video_id}/watched", methods=["POST"])
async def api_toggle_watched(request: Request) -> Response:
    video_id = request.path_params["video_id"]
    body = await request.json()
    watched = bool(body.get("watched", True))
    conn = _db()
    conn.execute("UPDATE videos SET watched = ? WHERE id = ?", (1 if watched else 0, video_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/videos/{video_id}/starred", methods=["POST"])
async def api_toggle_starred(request: Request) -> Response:
    """Body {"starred": true|false} — a plain bookmark flag, independent of
    watched state and interest_score, so a starred video stays easy to find
    for a later review pass even after you've watched and marked it."""
    video_id = request.path_params["video_id"]
    body = await request.json()
    starred = bool(body.get("starred", True))
    conn = _db()
    conn.execute("UPDATE videos SET starred = ? WHERE id = ?", (1 if starred else 0, video_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/videos/{video_id}/score", methods=["POST"])
async def api_set_score(request: Request) -> Response:
    """Body {"score": 1-5} to set, {"score": null} to clear. Purely
    subjective — never inferred, always set by the user."""
    video_id = request.path_params["video_id"]
    body = await request.json()
    score = body.get("score")
    if score is not None:
        score = int(score)
        if not 1 <= score <= 5:
            return JSONResponse({"error": "score must be 1-5 or null"}, status_code=400)
    conn = _db()
    conn.execute("UPDATE videos SET interest_score = ? WHERE id = ?", (score, video_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/videos/{video_id}/labels", methods=["POST"])
async def api_set_labels(request: Request) -> Response:
    """Body {"labels": ["ai_tech", "programming"]} — manual override of the
    auto-classified labels (replaces them entirely, doesn't merge)."""
    video_id = request.path_params["video_id"]
    body = await request.json()
    labels = body.get("labels", [])
    conn = _db()
    conn.execute("UPDATE videos SET labels = ? WHERE id = ?", (",".join(labels), video_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/labels/backfill", methods=["POST"])
async def api_labels_backfill(request: Request) -> Response:
    """Re-runs the keyword classifier over every row (not just NULL ones) so
    edits to LABEL_CATEGORIES take effect on already-synced videos without
    waiting for a resync. Manual overrides made via /labels are overwritten —
    that's a deliberate tradeoff for a personal single-user feed, not
    something to build reconciliation for."""
    conn = _db()
    rows = conn.execute("SELECT id, title, content FROM videos").fetchall()
    updated = 0
    for video_id, title, content in rows:
        labels = ",".join(_classify_labels(title or "", content or ""))
        conn.execute("UPDATE videos SET labels = ? WHERE id = ?", (labels, video_id))
        updated += 1
    conn.commit()
    conn.close()
    return JSONResponse({"updated": updated})


@mcp.custom_route("/api/videos/{video_id}/unfollow_creator", methods=["POST"])
async def api_unfollow_creator(request: Request) -> Response:
    """Unfollows/unsubscribes from this video's creator on their actual
    platform — a real, hard-to-reverse action on the live account, not just
    a local DB change. On success, also removes the creator from the local
    `creators` table so the sidebar reflects it immediately; already-synced
    videos from them are left alone (historical data isn't undone)."""
    video_id = request.path_params["video_id"]
    conn = _db()
    row = conn.execute("SELECT platform, user FROM videos WHERE id = ?", (video_id,)).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "Unknown video"}, status_code=404)
    platform, creator = row

    if platform == "youtube":
        result = await youtube.unsubscribe(video_id)
    else:
        result = await _unfollow_douyin_creator(video_id)

    if result.get("ok"):
        conn = _db()
        conn.execute("DELETE FROM creators WHERE platform = ? AND name = ?", (platform, creator))
        conn.commit()
        conn.close()
    return JSONResponse(result)


@mcp.custom_route("/api/creators", methods=["GET"])
async def api_creators(request: Request) -> Response:
    platform_param = request.query_params.get("platform")
    conn = _db()
    query = (
        "SELECT c.name, c.platform, c.avatar_url, c.unread_count, "
        "(SELECT COUNT(*) FROM videos v WHERE v.user = c.name AND v.platform = c.platform "
        "AND v.watched = 0 AND v.filtered_category IS NULL) AS unwatched_synced "
        "FROM creators c"
    )
    params: list = []
    if platform_param:
        query += " WHERE c.platform = ?"
        params.append(platform_param)
    query += " ORDER BY unwatched_synced DESC, c.name ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    creators = [
        {"name": r[0], "platform": r[1], "avatar_url": r[2], "unread_count": r[3], "unwatched_synced": r[4]}
        for r in rows
    ]
    return JSONResponse({"creators": creators})


@mcp.custom_route("/api/play/{video_id}", methods=["GET"])
async def api_play(request: Request) -> Response:
    """Proxy a video's play_url with the Referer header its CDN requires.

    Confirmed 2026-07-28: unlike thumbnails (which hotlink freely), Douyin's
    video CDN 403s a bare request but accepts one with
    Referer: https://www.douyin.com/ — a <video src=...> pointed straight at
    the CDN would send our own page's origin as Referer instead, so this
    proxy exists purely to set that header server-side. Forwards Range
    requests so the browser's scrubber/seek still works.
    """
    video_id = request.path_params["video_id"]
    conn = _db()
    row = conn.execute("SELECT play_url FROM videos WHERE id = ?", (video_id,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return JSONResponse({"error": "no play_url for this video"}, status_code=404)
    play_url = row[0]

    headers = {
        "Referer": "https://www.douyin.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    }
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
    upstream = await client.send(
        client.build_request("GET", play_url, headers=headers), stream=True
    )

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    passthrough_headers = {}
    for h in ("content-type", "content-length", "content-range", "accept-ranges"):
        if h in upstream.headers:
            passthrough_headers[h] = upstream.headers[h]
    passthrough_headers.setdefault("accept-ranges", "bytes")

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=passthrough_headers
    )


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8082)
