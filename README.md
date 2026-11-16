# myfollows

A self-hosted follow-feed browser for Douyin **and YouTube subscriptions**.
Logs into your accounts via Playwright, syncs recent videos into a local
SQLite database, and serves a web UI to browse thumbnails, watch videos
inline, and track watched/unwatched state — all without needing Claude or
any other LLM at runtime.

## Overview

- **douyin-mcp** (Docker, port 8082) — a Python server (FastMCP/Starlette,
  `server.py` + `common.py` + `youtube.py`) wrapping Playwright/Chromium
  sessions against douyin.com and youtube.com, plus a REST API and the web
  UI, all on the same port. (The directory is still named `douyin-mcp` for
  history's sake; it now serves both platforms.)
- **The web UI at `http://localhost:8082/` is the whole app.** It shows a
  thumbnail grid of videos from both platforms (filterable by platform or
  creator), lets you mark videos watched/unwatched, play videos inline, and
  trigger a sync — entirely self-service from your browser.
- Videos and creators from both platforms live in one local SQLite DB
  (`data/videos.db`), distinguished by a `platform` column.
- **Douyin login** is fully headless and self-service — the UI shows a QR
  code to scan with your phone.
- **YouTube login is different**: Google blocks automated credential entry,
  so there's no QR flow. Instead the UI's "Login to YouTube" button opens a
  real, interactive Chromium window directly on your desktop (the container
  passes through your host's own X display) that you drive yourself — see
  "YouTube login" below. Only works because the container runs on the same
  machine as the desktop it pops up on.
- The server also exposes `@mcp.tool()` functions and an `/mcp` endpoint
  (FastMCP's built-in MCP protocol support) purely as an optional
  convenience — a Claude Code session *can* trigger login/sync from chat if
  ever wanted, but nothing in normal operation needs it. See "MCP tools"
  below if you want to wire it into a Claude Code project's `.mcp.json`.

## Interface

MyFollows uses an icon and compact wordmark. Settings opens a full page with
Feed preferences, Channels & creators, and Accounts & maintenance sections;
Back to feed, Escape and browser navigation return to browsing. Creator search
filters the local creator list independently of feed search. Choose a creator
to return to their videos, or use Unlist/Relist to manage feed visibility.
Unwatched, starred, watch-later, platform and label filters plus recommendations
live in settings. Search and Sync stay visible in the main header.

Phones use larger touch targets, safe-area spacing and a single-column settings
layout with ordinary page scrolling. Playback adapts to portrait and landscape;
laptops/desktops use the available browser window without entering browser
fullscreen. The expand button or F hides/restores the description to give the
video more space while retaining access to native playback and rating controls. Landscape YouTube videos
fit the available viewport without the former 960px cap.

The player uses plain caption text aligned with the video's left edge and
places the action rail just below midpoint. Progress and seeking use the native
YouTube or browser video controls; there is no separate app timeline. P pauses
playback and reveals access to those controls; Space resumes. YouTube controls
use controls=1; their final visibility is managed by YouTube itself, not an
unsupported show-controls API.

UI styling and responsive breakpoints live in `douyin-mcp/ui.html`; no frontend
build step or new dependency is required. Rebuild the image to serve changes.

The homepage fetches 50 videos initially and appends the next 50 near the
bottom. A Load more/Retry button also supports keyboard use and failed requests.
Search runs over the full filtered library before pagination, including Unicode
creator names. Changing search or filters resets the cursor; stale requests are
aborted and ignored. Recommendations retain their bounded top-40 list. Player
navigation fetches the next batch at the boundary and keeps its own queue when
watched videos leave the grid. Thumbnails remain browser-lazy-loaded.

The list API accepts optional `limit` (1–200), opaque `cursor` from the previous
response, and literal `q` search. Paginated responses include `has_more` and
`next_cursor`. Ordering uses published date then video ID so equal timestamps
and watched removals do not shift later pages. Requests without `limit` retain
the legacy full-list contract. New sync arrivals appear on refresh. The UI batch
size is the product constant `FEED_BATCH_SIZE` in `ui.html`.

The creator name beneath playback links to the original video; there is no
separate “Open on…” row. Star, watch-later and tag controls use consistent inline
SVG icons, accessible labels and visible pressed states with touch-sized targets.

## Sync and playback reliability

The header Sync button calls `POST /api/sync/start`, which returns HTTP 202
immediately and runs Douyin then YouTube in one server-owned daemon thread.
Closing/reloading the browser does not cancel it. Repeated starts return the
existing job; startup and scheduled syncs share the coordinator and skip overlap.
No job queue or new service is introduced. A server restart interrupts active work
and resets its process-local status; retrying upserts safely into the existing
SQLite database. Existing blocking platform APIs/MCP tools retain their contracts
and remain outside this combined-job deduplication.

Detailed status lives in Settings → Sync. “New videos added” counts new database
rows from that platform, “videos fetched” includes existing rows, and “Library”
is the shared total across both platforms at the last update. Failed operations
without a result show unavailable counts, not zero. Manual and scheduled jobs use
`SYNC_LIMIT` from `settings.ini` (environment overrides apply); the optional start
endpoint `limit` parameter accepts 1–500. This change removes UI blocking, not
the external platforms’ scraping latency.


Automatic sync runs at startup, every 60 minutes after a run finishes, and at
06:00 in the container timezone. Each platform fails independently; failed or
partial runs remain visible through `GET /api/sync/status` and the UI polls
that endpoint every 2.5 seconds while a sync is active and every 15 seconds
while idle, pausing while hidden. Cached videos remain usable
without waiting for login or sync. Status is process-local and resets on restart;
manual and scheduled runs share the same background coordinator and status endpoint.

The YouTube sync reads `/feed/channels` separately from recent uploads, so
quiet subscribed channels can enter the sidebar. Collection is bounded by the
existing scroll limits and is additive: incomplete pages never delete creators.
A channel-list failure saves collected videos and reports an incomplete sync.
Douyin also saves scraped creators when its video feed is empty. Neither
platform guarantees an exhaustive inventory; selectors and page behavior still
need validation against a live account.

Douyin playback automatically refreshes its link once on a media error; a
second failure offers manual retry and the source link. Switching or closing
videos invalidates pending refresh results. Videos become watched only when
playback starts (Douyin `playing`, YouTube player-state messages), rather than
when opening a card. The existing 1.75× speed and single-video repeat remain.
YouTube timeouts show only a small source-link hint and never hide or reload
the iframe: missing cross-origin messages cannot establish a playback failure.
Only explicit player errors show a fallback. Embedded playback can still be
restricted by YouTube. Event meanings follow the
[YouTube IFrame API reference](https://developers.google.com/youtube/iframe_api_reference).

Edit `douyin-mcp/settings.ini` for the sync interval, daily hour, fetch limit,
port, log location and retention. Matching uppercase environment variables
override the file; integer settings are validated at startup. Relative log
paths resolve against the source directory, independently of the launch cwd.
`DATA_DIR`, browser session paths and `YOUTUBE_PROXY_SERVER` retain their existing
environment configuration. Rebuild the image after changing the shipped settings
or application source. A port change also requires updating the Dockerfile's
healthcheck and clients; it is not a deployment-wide automatic migration.

Operational logs append to `logs/YYYY-MM-DD.log` in UTC, with 14-day retention.
Compose persists `/app/logs` to `./logs` at the repository root. Only payload-free
startup and background sync operation events are written there; browser/library
diagnostics still go to the console. Generate a local seven-day report with:

```bash
python3 scripts/sync_report.py --days 7 --output /tmp/myfollows-sync-report.html
```

Open the generated HTML locally. It shows daily per-platform run counts,
failure rate, mean duration and fetched-item counts, plus missing/malformed log
coverage. Partial results count as failures. Background manual, startup and
scheduled runs are included; legacy blocking API calls and retries are excluded.
Reads are limited to 31 days and 8 MiB per day. The report contains aggregate
metrics and never includes raw log lines.

The inspected layout keeps platform code and UI together in `douyin-mcp/`,
maintenance commands in `scripts/`, checks in `tests/`, and mutable data in the
ignored `data/` and `logs/` directories. No existing file moves were needed for
these sync/playback fixes; `runtime.py` owns shared settings, operation logging
and scheduler state without importing platform adapters.

| Role | Implementation / source of truth | Replacement boundary |
| --- | --- | --- |
| API and server | FastMCP/Starlette, `douyin-mcp/requirements.txt`, `server.py` | A framework change must preserve REST shapes, streaming and MCP routes. |
| Platform collection | Playwright, `server.py` / `youtube.py` | Selector/API changes require adapter code and live verification; session formats are browser-specific. |
| Storage | SQLite, `common.py`, `DATA_DIR` | A database engine change needs SQL/schema adaptation and explicit data migration. |
| Playback/UI | Native video, YouTube iframe, `ui.html` | A provider change needs player/event integration; changing a URL alone is insufficient. |
| Scheduling/operations | Standard library, `runtime.py`, `settings.ini` | Timing settings are value changes; external scheduling must disable/replace the internal loop to avoid duplicates. |

Offline validation (no saved sessions or production database required):

```bash
python3 -m unittest discover -s tests -v
node tests/playback.test.cjs
```

## Running it

```bash
docker compose up -d
```

First run builds the image (Playwright + Chromium, ~1-2GB). Persists to
`data/` (gitignored — the `*storage_state.json` files hold live session
cookies, never commit them):
- `storage_state.json` — Douyin login session
- `youtube_storage_state.json` — YouTube/Google login session
- `videos.db` — SQLite: synced videos + watched state (both platforms)
- `qrcode.png` — most recent Douyin login QR (regenerated each login attempt)

Then open `http://localhost:8082/` — the page handles Douyin login (shows
the QR, polls for scan completion) and syncing itself. Use the "Login to
YouTube" button for the YouTube session (see below); both are optional
independently of each other.

Network access to douyin.com/youtube.com from this host is required, and
Douyin runs anti-bot risk-control that can outright block a session — see
the CAPTCHA note below.

### Login troubleshooting

If Douyin login returns `{"status": "error", "reason": "captcha_wall"}`:
Douyin served a `验证码中间页` CAPTCHA interstitial before any page JS ran —
server-side IP/fingerprint risk-control, not a bug, and no retry-immediately
or client-side flag fixes it. Confirmed 2026-07-28 on a flagged sandbox
network; resolved by running the container from a normal residential/office
network. A `reason: stale_selector` error instead means Douyin's login DOM
changed — fix `QR_SELECTOR`/`LOGGED_IN_SELECTOR` in `server.py`.

## YouTube login

Google actively blocks automated sign-in (the classic "This browser or app
may not be secure" wall), and there's no public QR-based login like
Douyin's — so this can't be a fully headless, self-service flow the way
Douyin's is. Two ways to get a session into `data/youtube_storage_state.json`:

### Option A: interactive login via a real desktop window

1. Click **"Login to YouTube"** in the UI. This launches a real, headed
   Chromium **directly on your own desktop** — the container passes
   through your host's X display (`/tmp/.X11-unix` mount + `DISPLAY=:0` in
   `docker-compose.yml`) instead of running a virtual display, so what
   appears is an ordinary Chromium window on your taskbar, pointed at
   Google's sign-in page. This only works because the container runs on
   the same machine as the desktop it pops up on — a headless remote host
   would need a different approach (real OAuth, most likely).
2. In that window, sign in normally — email, password, 2FA/passkey,
   whatever your account requires. This is a real interaction with
   Google's actual login UI, not automation, so it isn't treated as bot
   activity.
3. Once `youtube.py` detects the logged-in avatar button, it saves the
   session to `data/youtube_storage_state.json` and closes the window.
   From then on, syncs reuse that saved session headlessly — no window
   needed again unless the session expires.

If the window doesn't appear: confirm `xhost` on the host allows local
root connections (`xhost` should list `SI:localuser:root`, the default on
most desktop distros) and that `/tmp/.X11-unix` is actually mounted into
the container (`docker-compose.yml`). If a login window is left open from
a previous attempt, clicking "Login to YouTube" again closes it and opens
a fresh one (`youtube.py`'s `_get_headed_context`).

### Option B: import your local Chrome's session

If you're already logged into YouTube in your own Chrome on this machine,
`scripts/import_youtube_cookies.py` skips the interactive step entirely:
it reads `.youtube.com`/`.google.com` cookies straight out of Chrome's local
cookie DB (decrypting them via the OS keyring — Secret Service D-Bus API),
writes them into `data/youtube_storage_state.json` in Playwright's format,
and pings the running container so it picks up the change immediately (no
restart needed). Run it from the repo root on the host (not in the
container):

```
python3 scripts/import_youtube_cookies.py
```

**This file is as sensitive as a password** — the imported cookies (SID,
SAPISID, etc.) are your whole Google account session, not scoped to
YouTube. It's written `0600` and already covered by `.gitignore`, same as
the other `*storage_state.json` files.

Google rotates some of these cookies as your real Chrome session keeps
being used, so an imported session can go stale — if a sync suddenly
reports `"Session looks logged out"`, just re-run the script.

### Troubleshooting

If YouTube's subscriptions feed comes back empty while logged in,
YouTube's DOM likely changed — inspect
`https://www.youtube.com/feed/subscriptions` with devtools and update
`VIDEO_CARD_SELECTOR`/`_scrape_subscriptions` in `youtube.py`.

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
| `youtube_login_status()` | `{logged_in: bool}` |
| `youtube_login_start()` | Opens an interactive Chromium window directly on the host desktop, at Google sign-in. Returns `{status: "window_opened"}` — a human needs to actually sign in in that window; this can't be automated |
| `youtube_login_wait(timeout_sec=180)` | Blocks until the interactive login completes, then persists the session |
| `youtube_sync_feed(limit=30)` | Fetches recent videos from `youtube.com/feed/subscriptions`, upserts into the DB. Returns `{fetched, new, total_in_db, creators}` |

## REST API (same port, used by the UI)

| Endpoint | Purpose |
|---|---|
| `GET /` | The UI page |
| `GET /api/status` | Douyin: `{logged_in, in_progress}` — cheap poll, doesn't navigate away from an in-progress login |
| `POST /api/login/start` | Starts Douyin login, saves QR |
| `GET /api/login/qr.png` | The current Douyin QR image |
| `POST /api/sync/start?limit=100` | Start combined background sync; HTTP 202, or existing job if already running. |
| `GET /api/sync/status` | Fast process-local combined job and per-platform status. |
| `POST /api/sync?limit=30` | Same as `douyin_sync_feed` |
| `POST /api/backfill?limit=50` | Same as `douyin_backfill_play_urls` |
| `GET /api/youtube/status` | Same as `youtube_login_status`/poll — `{logged_in, in_progress}` |
| `POST /api/youtube/login/start` | Same as `youtube_login_start` — returns `{status: "window_opened"}` |
| `POST /api/youtube/sync?limit=30` | Same as `youtube_sync_feed` |
| `GET /api/videos?watched=false&show_filtered=false&user=&platform=&label=&starred=` | List videos (JSON), filterable by watched state, creator, `platform` (`douyin`/`youtube`), `label` (one of `label_categories` in the response), and `starred=true`. Each video includes `interest_score` (1-5 or null), `labels` (array), and `starred` (bool) |
| `POST /api/videos/{id}/watched` | Body `{"watched": true\|false}` |
| `POST /api/videos/{id}/score` | Body `{"score": 1-5\|null}` — subjective, always user-set, never inferred |
| `POST /api/videos/{id}/labels` | Body `{"labels": ["ai_tech", ...]}` — manual override of auto-classified labels |
| `POST /api/videos/{id}/starred` | Body `{"starred": true\|false}` — plain bookmark flag, independent of watched/score, for a later review pass |
| `POST /api/videos/{id}/watch_later` | Body `{"watch_later": true\|false}` — queue flag for long videos, independent of starred/score |
| `GET /api/videos/recommended?limit=40` | Top `limit` unwatched videos ranked by `recommend_score` (blends interest_score, starred, watch_later, and label affinity) — see "Recommended Top 40" below |
| `POST /api/labels/backfill` | Re-runs the keyword classifier (see "Interest labels" below) over every row, overwriting any manual label edits |
| `GET /api/creators?platform=` | Creators (name, avatar, unread/unwatched counts) for the sidebar, optionally filtered by platform |
| `GET /api/play/{id}` | Proxies a **Douyin** video's actual playable stream (see below) — YouTube videos instead play via the official `youtube.com/embed/{id}` iframe, no proxying needed |
| `POST /api/videos/{id}/refresh_play_url` | Re-fetches this one video's `play_url` even if already populated — for a 403 caused by an *expired* signed CDN URL, not a missing one (see gotcha below); `/api/backfill` only targets rows where `play_url` is empty |
| `POST /api/videos/{id}/unfollow_creator` | Unfollows (Douyin) / unsubscribes (YouTube) from this video's creator — a real, hard-to-reverse action on the live account, see "Unfollowing/unsubscribing" below |

## Keyboard shortcuts (ui.html)

Grid (player closed):
- `Space` — open/play the first video in the current filtered list
- `m` — mark the first video watched; `<N>m` (e.g. `5m`, vim-style count
  prefix) marks the first N. Digit buffer resets after 1.5s or any
  non-digit/non-m key.
- `<N>i` (N = 1-5, e.g. `3i`) — sets the **first** video's interest score to
  N. Unlike `<N>m`, the digit here is the score itself, not a count — bare
  `i` with no digit buffered does nothing (no sensible default score).
  Clicking a star on a card directly, or clicking the same score again,
  toggles it back to unscored.

Player (open):
- `1`–`5` — score the current video; the rating dialog shows five clickable stars followed by a single matching description (Not interesting / Fine / Great / Excellent / Unbelievable). Text fields retain normal number entry.
- `Space` — pause/resume
- `↑` — volume +10%, clamped at 100%
- `↓` — next video; pauses and opens Rate & tag if the current video needs a rating or label. After filling both in, press `↓` again to continue.
- `←`/`→` — seek ±5s
- `p` — pause and reveal native playback controls/timeline; `Space` resumes
- `f` — expand the video within the browser window by hiding the description; press again to restore it. No browser fullscreen is requested.
- `Escape` / click outside / ✕ — close

The player fills the browser window without requesting browser fullscreen.
Plain title/channel text sits below the video, aligned to its left edge, with
no panel background or border. Desktop descriptions are capped at 96px;
long descriptions scroll without navigating videos. P pauses and uncovers native
controls. Space resumes, including when the native video controls have focus.
The separate custom progress bar has been removed.

While the player is open, an early window-capture handler owns unmodified Space
on keydown, keypress and keyup, so document-level speed-controller extensions
cannot toggle playback a second time. Text inputs and modified shortcuts keep
their normal behavior; the tagging dialog still uses Space to select tags.
The rail includes a clickable Play / pause button as a fallback. A webpage
cannot override an extension that has already stopped the event in an earlier
window-capture listener, or intercept keys inside a cross-origin YouTube frame.
Those cases may require changing the extension's Space shortcut.
Offline conflict checks: `python3 tests/keyboard_conflict_browser.py` uses
simulated extension handlers, not the user's browser profile.

In Rate & tag, h/l moves between tag chips and j/k moves between wrapped rows. Space toggles the focused tag; 1–5 selects a score. Enter or **Save & next** waits for the rating and tags to save before advancing (both a score and at least one tag are required). Failed saves keep the dialog open for retry. Typed custom tags are committed with Enter or when leaving the input; Escape cancels the new tag. New tags are immediately added to the chip set and filter and are recovered from saved video labels on reload. The dialog has no Unlist action; the YouTube channel action reads **Unsubscribe channel**.

Offline browser verification: `python3 tests/tagging_browser.py` for tagging and `python3 tests/playback_browser.py` for window sizing and pause/resume (requires Python Playwright and its Chromium browser; all requests use synthetic fixtures).

Playback ends without looping and opens Rate & tag for the current video on both platforms. Text fields keep their normal keyboard behavior.

Playback defaults to 1.75x speed (`DEFAULT_PLAYBACK_RATE` in `ui.html`) —
set directly via `playerVideo.playbackRate` for Douyin's native `<video>`,
and via a `setPlaybackRate` postMessage command once YouTube's embed iframe
finishes loading (has to wait for `load`, not `openPlayer()`'s `src`
assignment, or the command lands before YouTube's own listener attaches and
is silently dropped — same constraint as the volume/play/pause commands).

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

**`#playerMark`/`#playerMeta` need an explicit `max-width`, not `width:100%`
or `align-self:stretch` — confirmed 2026-07-29 on a portrait (9:16) Douyin
video.** `#playerCard` is `width: fit-content`, and a flex-wrap row's
contribution to an ancestor's fit-content size is its *unwrapped*
max-content width — wrapping only kicks in once the container's width is
already constrained some other way. The label row has ~11 chips that, laid
out on one line, come to ~945px, so fit-content sized the whole modal to
that even when the actual video rendered at only ~534px wide — the video
looked squeezed into a narrow column while the marking controls below it
spilled out past its edges. `align-self:stretch` looked like a fix (matches
the container's resolved width instead of computing its own) but actually
made it worse in a different way: it fills from the flex start edge, so
under a *wide* landscape video the row would hug the left side instead of
sitting centered like the video above it. The actual fix is a plain
`max-width: min(90vw, 480px)` on both — that caps their own contribution to
`#playerCard`'s fit-content calculation (so labels wrap like they're
supposed to) while leaving `#playerCard`'s own `align-items: center` to
center them normally under videos of any aspect ratio.

**`.label-chip` needs `white-space: nowrap; flex-shrink: 0;`, and `.star`
needs `color: var(--text-dim)` not `var(--line)` — confirmed 2026-07-29,
both shipped visually broken in the same round as the max-width fix
above.** Without `flex-shrink:0`, a chip shrinks below its own text's width
once the row runs low on space (flex-shrink defaults to 1), and the text
wraps *inside* the chip instead of the whole chip wrapping to the next row
— "english learning" was rendering as "english" / "learning" stacked on
two lines rather than staying one pill. Separately, unfilled score stars
used `var(--line)` (`#25252b`) on `var(--panel)` (`#16161a`) — those two
colors are close enough to be effectively invisible, not just "dim"; an
unscored video's star row looked like it wasn't rendering at all. Fixed by
matching the visible-but-secondary treatment already used for `.user`/
`.date` elsewhere (`var(--text-dim)`).

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

## Interest labels + score (added 2026-07-29)

Separate from content filters (which hide things) — these are informational
tags to help mark and later get recommendations from unwatched videos, not
run at recommendation time by any LLM/model in the container.

- **`labels`** (`videos.labels`, comma-string in the DB / array over the
  API) — auto-assigned at sync time by `_classify_labels` /
  `LABEL_CATEGORIES` in `common.py`, a keyword match like `_classify_filter`
  but **not** mutually exclusive (a video can match several categories) and
  informational only (nothing is hidden). Current categories: `ai_tech`,
  `programming`, `english_learning`, `psychology`, `relationships`,
  `financial`, `math_science`, `explainer`, `fitness`, `news`, `relaxation`
  — chosen from what actually showed up in this feed, not a generic guess
  (`relationships` in particular was added after the first backfill showed
  an 11-video NPD/toxic-relationship cluster that didn't fit anywhere else);
  edit
  `LABEL_CATEGORIES` and call `POST /api/labels/backfill` to reclassify
  existing rows after changing keywords (no rebuild needed for a backfill,
  but a keyword *edit* to `common.py` does need
  `docker compose up -d --build` first).
- **`interest_score`** (`videos.interest_score`, 1-5 or null) — purely
  subjective, always set by hand, never inferred from content. This is the
  signal a recommendation pass (currently done ad hoc, not an in-app
  feature) uses alongside labels to rank unwatched videos.

**Marking UI lives in the player dialog, not the grid card** (moved there
2026-07-29): opening a video (`openPlayer`) renders a star row and a full
row of label chips below the title (`#playerMark` in `ui.html`,
`renderPlayerMark()`), so you mark interest while actually watching instead
of guessing from a thumbnail. Stars set `interest_score` (clicking the
already-active star clears it back to null); label chips show *every*
category (not just the auto-assigned ones) and toggle membership on click,
calling `POST /api/videos/{id}/labels` with the full updated array each
time — this is a manual override of the auto-classifier for that one video,
per the backfill note above. The `<N>i` grid-level shortcut (N=1-5, scores
the first visible video without opening the player) still works too.

A trailing **`+` chip** opens an inline text input (Enter to add, Escape/
blur to cancel) for one-off custom labels that aren't in
`LABEL_CATEGORIES` at all — e.g. tagging a specific video "must watch
twice" without adding a whole new global category for it. Custom labels
just live in that video's `labels` string like any other; `renderPlayerMark`
renders the *union* of `LABEL_CATEGORIES` and whatever's already on the
video specifically so a custom label still shows as an active chip on
reopen, even though it'll never appear in the top-bar label filter
dropdown (that's populated from `label_categories`, the static predefined
list only).

## Starring videos for later review (added 2026-07-29)

A third, separate marking dimension from score/labels: **`starred`**
(`videos.starred`, plain 0/1) — a bookmark flag with no scale and no
content classification, just "come back to this." Toggled via the
`☆ Star` / `★ Starred` button in the player dialog (`#playerStarToggle`,
top of `#playerMark`, next to the score stars) — wired once as a static
listener rather than rebuilt per `renderPlayerMark()` call like the score/
label controls, since it doesn't need to regenerate a chip list. A
**"Starred only"** checkbox in the header (next to "Show filtered") passes
`starred=true` to `GET /api/videos`, independent of the "Unwatched only"
checkbox — so a starred video stays easy to find in a later review pass
even after you've already watched it and it would otherwise have dropped
out of the default unwatched-only view.

## Unfollowing/unsubscribing from the player dialog (added 2026-07-29)

An `Unfollow` (Douyin) / `Unsubscribe` (YouTube) button sits under the
title in the player dialog (`#playerUnfollowBtn`). Unlike every other
marking feature in this doc, **this performs a real action on your actual
live Douyin/YouTube account** — not a local DB change — so the UI confirms
via a native `confirm()` dialog before firing the request, and the button
shows `Working…` while the Playwright automation runs (several seconds,
same order of magnitude as a login/sync).

**We don't store a stable per-creator profile ID.** Douyin's follow
sidebar (`_scrape_creators`) and YouTube's subscriptions feed
(`_scrape_subscriptions`) both only ever capture a display name — no
href/user-id/channel-id — so there's no stored link to jump straight to a
creator's profile. Both `_unfollow_douyin_creator` (server.py) and
`unsubscribe` (youtube.py) work around this by resolving the creator fresh
from the specific video you're watching: Douyin visits the video's own
page and reads `author.sec_uid` out of the intercepted `aweme/detail`
response (same technique as `_refetch_play_url`); YouTube opens the
video's watch page and follows its channel link. On success, the creator's
row is deleted from the local `creators` table (so they drop out of the
sidebar immediately) — already-synced videos from them are left alone,
since unfollowing undoes the *relationship*, not video history.

**Both are UNVERIFIED against a live logged-in session as of 2026-07-29** —
written from typical/documented DOM patterns, not confirmed against
Douyin's or YouTube's actual current markup (unlike e.g. `QR_SELECTOR`,
which was reverse-engineered from a real devtools session). Both functions
verify their own success — checking that the follow/subscribe button
actually changed state afterward — and report `{"ok": false, "error": ...}`
honestly rather than assuming a click that didn't error means it worked. If
one reports failure (or silently does nothing), that's the first place to
check: inspect a real followed-creator profile / subscribed-channel page
with devtools and update `DOUYIN_FOLLOWED_BUTTON_SELECTOR`/
`DOUYIN_UNFOLLOW_CONFIRM_SELECTOR` in `server.py` or `CHANNEL_LINK_SELECTOR`/
`SUBSCRIBE_BUTTON_SELECTOR`/`UNSUBSCRIBE_CONFIRM_SELECTOR` in `youtube.py`.

## Recommended Top 40 (added 2026-08-10)

For when the unwatched pile grows past what's actually browsable: a
**🎯 Recommended** button in the header (`#recommendedBtn`, next to "Watch
later only") switches the grid from the normal filtered list to the top 40
unwatched videos by a computed `recommend_score`, via `GET
/api/videos/recommended?limit=40` (`api_recommended` in `server.py`). While
active, the other filter controls are disabled (`setRecommendedMode` in
`ui.html`) since the recommended pool ignores them — it's always drawn from
unwatched, non-filtered-category, non-unlisted-creator videos, same base
set as the default grid view. Picking a creator from the sidebar exits
recommended mode automatically (`selectCreator`), since a single-creator
filter doesn't compose with a cross-feed ranking.

The score deliberately reuses the existing marking dimensions instead of
calling out to any model — per-video:
```
own interest_score * 2         (explicit, if this specific video was pre-scored)
+ 3 if starred                 (explicit "come back to this")
+ 2 if watch_later             (explicit "queued")
+ avg(interest_score) of other already-scored videos sharing a label
```
The label-affinity term is the only thing that generalizes past ratings to
videos that were never individually scored — it's computed once per request
over every row with a non-null `interest_score` (regardless of watched
state), then averaged per label. Ties — including the common cold-start
case where nothing has been marked yet, so every video scores 0 — fall back
to `published_at` desc, so the feature still does something useful (surface
the newest 40) before any marking has happened. Same JSON video shape as
`GET /api/videos` (plus `recommend_score`), so it reuses `card()`/
`openPlayer()`/keyboard shortcuts in `ui.html` unchanged.

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
- **Douyin's `play_url` is a short-lived signed URL, not a stable
  link — confirmed 2026-07-29.** It carries a `dy_q` unix-timestamp query
  param that expires roughly a day after the video was synced/backfilled;
  `/api/play/{id}` 403s once that passes even though `play_url` is
  populated (not null/empty), which is a *different* failure than the
  "never had a play_url" case above but the player's `error` event handler
  can't distinguish them client-side — both show the same `#playerFallback`
  message. `/api/backfill` (targets rows with no `play_url` at all) doesn't
  help here since the row already has one, just a stale one; use
  `/api/videos/{id}/refresh_play_url` instead, which always re-fetches
  regardless of the current value (wired to a "Refresh link" button in the
  fallback UI). There's no long-term fix for this short of resyncing more
  aggressively — it's inherent to how Douyin signs its CDN URLs.
- **SQLite schema changes need an explicit migration.** `CREATE TABLE IF
  NOT EXISTS` does not add columns to an already-existing table — adding
  `play_url` without an `ALTER TABLE ... ADD COLUMN` migration in `_db()`
  broke every sync with "table videos has no column named play_url" against
  the pre-existing `data/videos.db`. Any future column addition needs the
  same treatment (check `existing_cols` pattern in `_db()`).

## YouTube subscriptions sync (youtube.py)

Unlike Douyin's `/follow`, YouTube's `/feed/subscriptions` page is already
the exact "recent uploads from channels I'm subscribed to" view, sorted
newest-first — no per-channel enumeration or pagination-arrow clicking
needed, just scroll-to-load-more and scrape the `<ytd-rich-item-renderer>`
cards that appear (title, channel, video id, thumbnail, relative time text).

- **`published_at` is approximate.** The feed DOM only exposes relative
  text ("3 hours ago", "2 days ago"), not an exact timestamp —
  `_parse_relative_time` converts that to an ISO timestamp by subtracting
  from "now" at scrape time. Fine for newest-first ordering and display, not
  exact to the second. (Douyin's `create_time` field, by contrast, is an
  exact unix timestamp from the real API.)
- **No CDN proxy needed for playback** — YouTube videos play via the
  official `youtube.com/embed/{id}` iframe in the UI, so `play_url` is
  always left empty for YouTube rows; don't be surprised it's blank, that's
  expected (see `/api/play/{id}` note above, which is Douyin-only).
- **Creator avatars are left blank** — the subscriptions feed's per-video
  card doesn't reliably expose a per-channel avatar image the way Douyin's
  follow sidebar does, so YouTube creator rows just have no avatar (the UI
  hides the broken-image icon via `onerror`).
- **YouTube errors 101/150 mean embedding is disallowed.** On narrow screens (up to 860px), an Open in YouTube button is always present below the media for YouTube videos, even when the iframe sends no error event. Wide screens keep embedded playback without that extra footer button. The fallback also provides the link and hides Retry for these errors. The user returns manually to MyFollows to rate/tag; external playback completion cannot be observed by this app. Other errors retain Retry.

- **YouTube iframe silence is not evidence of bot blocking.** Playback may
  work while no state messages arrive. `armYtFallbackTimer` only adds a
  source-link hint after nine seconds; it must never hide, reload or replace
  the iframe. Explicit player errors may show the fallback. The regression
  checks in `tests/playback.test.cjs` cover silence and explicit errors.

## Rules of thumb

- **Never fabricate video data.** If a field can't be extracted, leave it
  empty rather than guessing.
- **Don't loop login attempts unattended** — Douyin's QR scan and YouTube's
  interactive desktop-window login both need a human present.
- **Selectors and the feed API will drift.** Treat an empty feed or missing
  QR as a maintenance signal — check `server.py`'s selector constants and
  `_parse_feed_response` (Douyin) or `youtube.py`'s `VIDEO_CARD_SELECTOR`/
  `_scrape_subscriptions` (YouTube) first, per the sections above.
