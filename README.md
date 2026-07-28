# myfollows

A self-hosted Douyin follow-feed browser. Logs into your Douyin account via
Playwright, syncs videos from creators you follow into a local SQLite
database, and serves a web UI to browse thumbnails, watch videos inline, and
track watched/unwatched state — all without needing Claude or any other LLM
at runtime.

## Overview

- **douyin-mcp** (Docker, port 8082) — a Python server (FastMCP/Starlette)
  wrapping a headless Playwright/Chromium session against douyin.com, plus a
  REST API and the web UI, all on the same port.
- **The web UI at `http://localhost:8082/` is the whole app.** It shows a
  thumbnail grid of videos from followed creators, lets you mark videos
  watched/unwatched, filter by creator, play videos inline, and trigger a
  sync — entirely self-service from your browser.
- Videos live in a local SQLite DB (`data/videos.db`).
- The server also exposes a few `@mcp.tool()` functions and an `/mcp`
  endpoint (FastMCP's built-in MCP protocol support) purely as an optional
  convenience — a Claude Code session *can* trigger login/sync from chat if
  ever wanted, but nothing in normal operation needs it. See "MCP tools"
  below if you want to wire it into a Claude Code project's `.mcp.json`.

## Running it

```bash
docker compose up -d
```

First run builds the image (Playwright + Chromium, ~1-2GB). Persists to
`data/` (gitignored — `storage_state.json` holds live session cookies, never
commit it):
- `storage_state.json` — Douyin login session
- `videos.db` — SQLite: synced videos + watched state
- `qrcode.png` — most recent login QR (regenerated each login attempt)

Then open `http://localhost:8082/` — the page handles login (shows the QR,
polls for scan completion) and syncing itself.

Network access to douyin.com from this host is required, and Douyin runs
anti-bot risk-control that can outright block a session — see the CAPTCHA
note below.

### Login troubleshooting

If login returns `{"status": "error", "reason": "captcha_wall"}`: Douyin
served a `验证码中间页` CAPTCHA interstitial before any page JS ran —
server-side IP/fingerprint risk-control, not a bug, and no retry-immediately
or client-side flag fixes it. Confirmed 2026-07-28 on a flagged sandbox
network; resolved by running the container from a normal residential/office
network. A `reason: stale_selector` error instead means Douyin's login DOM
changed — fix `QR_SELECTOR`/`LOGGED_IN_SELECTOR` in `server.py`.

## MCP tools (douyin-mcp) — optional, for Claude Code

Not required for normal use — the UI's "Sync now"/"Fix old links" buttons
and self-service login cover everything. Wire `http://localhost:8082/mcp`
into a project's `.mcp.json` only if you want to trigger these from a Claude
Code chat session.

| Tool | Purpose |
|---|---|
| `douyin_login_status()` | `{logged_in: bool}` |
| `douyin_login_start()` | Opens douyin.com, screenshots the login QR to `data/qrcode.png` |
| `douyin_login_wait(timeout_sec=90)` | Blocks until the QR is scanned, then persists the session |
| `douyin_sync_feed(limit=30)` | Fetches recent followed-creator videos + the creator sidebar list, upserts into the DB. Returns `{fetched, new, total_in_db, creators}` — not the video list, since browsing happens in the UI |
| `douyin_backfill_play_urls(limit=50)` | Fetches playable links for rows synced before that field existed (one page load per video, ~2-3s each — slower than a sync, call repeatedly until `remaining` is 0). Also a "Fix old links" button in the UI |

## REST API (same port, used by the UI)

| Endpoint | Purpose |
|---|---|
| `GET /` | The UI page |
| `GET /api/status` | `{logged_in, in_progress}` — cheap poll, doesn't navigate away from an in-progress login |
| `POST /api/login/start` | Starts login, saves QR |
| `GET /api/login/qr.png` | The current QR image |
| `POST /api/sync?limit=30` | Same as `douyin_sync_feed` |
| `POST /api/backfill?limit=50` | Same as `douyin_backfill_play_urls` |
| `GET /api/videos?watched=false&show_filtered=false&user=` | List videos (JSON), filterable |
| `POST /api/videos/{id}/watched` | Body `{"watched": true\|false}` |
| `GET /api/creators` | Followed creators (name, avatar, real Douyin unread count, our unwatched-synced count), for the sidebar |
| `GET /api/play/{id}` | Proxies the video's actual playable stream (see below) — this is what the UI's popup player points `<video src>` at, not the CDN URL directly |

## Keyboard shortcuts (ui.html)

Grid (player closed):
- `Space` — open/play the first video in the current filtered list
- `m` — mark the first video watched; `<N>m` (e.g. `5m`, vim-style count
  prefix) marks the first N. Digit buffer resets after 1.5s or any
  non-digit/non-m key.

Player (open):
- `Space` — pause/resume
- `↑`/`↓` — playback speed ±0.25×, clamped [0.25×, 3×]
- `←`/`→` — seek ±5s
- `Escape` / click outside / ✕ — close

All driven by one `document` keydown listener that branches on whether
`#playerOverlay` has the `show` class — don't add a second listener, extend
the existing branch. Space is intentionally context-dependent (grid vs
player) since native browser video controls don't reliably have keyboard
focus when the player opens via a button click elsewhere on the page —
confirmed 2026-07-28, native pause wasn't responding for this reason, fixed
by handling Space explicitly rather than relying on it.

**Player sizing gotcha**: `#playerCard video` must keep `width: auto;
height: auto;` with `max-width`/`max-height` doing the capping, plus
`aspect-ratio: 9/16` as a placeholder only (to avoid a small-then-grows pop
when the modal opens, before real metadata loads). Setting an explicit
`height` (not auto) broke sizing for any video whose real ratio isn't
9:16 — confirmed 2026-07-28, this regressed once already after a
well-intentioned "fix" for the pop-in. If touching this CSS, verify against
a video with `videoWidth`/`videoHeight` that isn't 1080×1920, not just the
first one you happen to click.

**CSS `aspect-ratio` does NOT get overridden by real intrinsic size —
confirmed the opposite of what's intuitive, cost a full debugging round
2026-07-28.** Per spec, a *specified* (non-`auto`) `aspect-ratio` value on a
replaced element wins over the element's natural ratio, always — the
natural ratio only applies as a fallback when the CSS property is literally
`auto`. So the `aspect-ratio: 9/16` placeholder was silently force-fitting
*every* video (landscape ones included) into a portrait box, and the real
footage then letterboxed a second time inside that wrong box — a landscape
16:9 video was rendering at ~26% viewport width instead of ~83%. This is
NOT a caching artifact (checked and ruled out first, then added
`Cache-Control: no-store` on `/` anyway since it had none). Fix:
`playerVideo`'s `loadedmetadata` listener sets `style.aspectRatio =
"${videoWidth} / ${videoHeight}"` once real dimensions are known (inline
style beats the CSS class rule), and `openPlayer()` clears that inline
style (`style.aspectRatio = ''`) before loading the next video so it starts
from the 9:16 placeholder again. If you ever add a new `aspect-ratio`
placeholder anywhere in this UI, it needs the same real-dimensions-override
pairing or it will have this exact bug.

## Content filters

Videos primarily about these topics are auto-tagged and hidden from the
UI's default view (still in the DB, visible via "Show filtered"):
- Drawing / painting (绘画, 画画, 美术, 手绘, 素描, 水彩, 马克笔, 临摹)
- Gym / workout (健身, 胸肌, 背肌, 腹肌, 深蹲, 卧推, 撸铁, 增肌, "workout", "gym ")

Implemented as a keyword match in `_classify_filter` / `FILTER_CATEGORIES`
in `server.py` (title+content, case-insensitive) — deterministic, since
sync happens from the UI's "Sync now" button, not via per-video LLM
judgment. When adding/removing a category, edit `FILTER_CATEGORIES` and
rebuild (`docker compose up -d --build`).

## How the feed sync actually works (read before touching `server.py`)

Confirmed 2026-07-28, don't relitigate without checking devtools first:

- **`/follow` is a single-video story-style player**, not a scrollable card
  grid — one followed creator's video at a time, a "next" arrow advances,
  the URL never changes. DOM scraping doesn't apply here.
- **The real data source is the JSON API** the page itself calls,
  `/aweme/v1/web/follow/feed/`, fired on page load and again on each "next"
  click (cursor-based pagination, no URL change). `server.py` intercepts
  these responses (`_parse_feed_response`) rather than scraping the DOM —
  far more stable, and gives an exact `create_time` unix timestamp, a real
  `share_url`, and a hotlinkable `video.cover.url_list[0]` thumbnail
  (confirmed: no Referer/auth needed, direct `<img src>` works).
  Clicking "next" is what drives pagination forward — a single page load
  only returns the first batch (~6 items).
- **The feed is non-deterministic across calls.** Two back-to-back syncs
  can surface different videos — it's ranked/personalized, not strict
  reverse-chronological. Upserting on the DB (keyed by video id) is what
  makes repeated syncs safe and eventually-complete; no single sync is
  exhaustive.
- If a sync returns an error about the API shape changing, or the UI shows
  an empty grid while logged in: inspect a live `/aweme/v1/web/follow/feed/`
  response in devtools and update `_parse_feed_response`.
- **The followed-creator sidebar** (`_scrape_creators`) is DOM-scraped, not
  API-intercepted, unlike the feed — the sidebar's `<li>` items have no
  href/user-id, only display text (name + optional "N个作品未看" unread
  line) and an avatar `<img>`. Name is used as the natural key; fragile if
  two followed accounts share a display name, acceptable for a personal
  follow list. Runs as part of `_run_sync`, reusing the same `/follow` page
  load rather than a separate navigation.
- **The sidebar lazy-loads ~40 `<li>` at a time** — confirmed 2026-07-28
  with 126 followed creators, only 40 were in the DOM until scrolled (it's
  append-only, not windowed — already-rendered items stay put, so this is
  safe to just keep scrolling). `_scroll_load_all_creators` handles this,
  scrolling `ul.VifoXYqW`'s actual scroll ancestor (`.closest('.MkTWFeTR')`,
  not the `<ul>` itself) to `scrollHeight` repeatedly. Two non-obvious
  requirements found by trial: small fixed-increment scrolls (e.g. +600px)
  don't reliably cross whatever threshold triggers the next batch — jump to
  `scrollHeight` (bottom) each time instead; and breaking on the first
  no-growth poll is too eager since the count can read flat for one cycle
  right after a scroll before the next batch renders — wait for
  `CREATOR_LIST_STALL_LIMIT` (3) consecutive flat reads before giving up.
- **Video playback needs a Referer-header proxy; thumbnails don't.**
  Confirmed 2026-07-28: `video.cover.url_list[0]` (thumbnail) hotlinks with
  zero headers, but `video.play_addr_h264`/`play_addr` 403s without
  `Referer: https://www.douyin.com/` — a bare `<video src=...>` pointed at
  the CDN sends the *page's own* origin as Referer, so it 403s too. The UI's
  player points at `/api/play/{id}` instead, which proxies the real CDN
  request server-side with the right header (and forwards `Range` for
  seeking — confirmed 206 Partial Content responses work). Rows synced
  before this existed have no `play_url`; the UI shows a fallback message
  + "Open on Douyin" link rather than a dead black video box (check
  `#playerFallback` in `ui.html` before assuming a playback bug is new).
- **SQLite schema changes need an explicit migration.** `CREATE TABLE IF
  NOT EXISTS` does not add columns to an already-existing table — adding
  `play_url` without an `ALTER TABLE ... ADD COLUMN` migration in `_db()`
  broke every sync with "table videos has no column named play_url" against
  the pre-existing `data/videos.db`. Any future column addition needs the
  same treatment (check `existing_cols` pattern in `_db()`).

## Rules of thumb

- **Never fabricate video data.** If a field can't be extracted, leave it
  empty rather than guessing.
- **Don't loop login attempts unattended** — a QR scan needs a human
  present.
- **Selectors and the feed API will drift.** Treat an empty feed or missing
  QR as a maintenance signal — check `server.py`'s selector constants and
  `_parse_feed_response` first, per the section above.
