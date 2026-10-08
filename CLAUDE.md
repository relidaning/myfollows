# CLAUDE.md

This repo's README.md is the primary living doc — architecture, REST/MCP API
tables, and dated debugging gotchas all live there. Read it before making
changes, especially "How the feed sync actually works" and "YouTube
subscriptions sync" sections. This file only holds things not obvious from
README/code.

## Operational hazards

- **Port collision risk from `network_mode: host`.** This container binds
  8082 directly on the host with no Docker-level port isolation. If another
  unrelated container also uses `network_mode: host` and happens to bind
  8082, they'll silently fight over the port — symptoms are intermittent
  404s/500s/hangs that look like app bugs but aren't. Confirmed 2026-07-29:
  a totally different project's container (`douyin-follows-douyin-mcp-1`)
  was squatting 8082 and answering some requests meant for this app. Check
  `docker ps` for other containers on the same ports before deep-diving a
  "flaky" bug here. Note the daily scheduled sync (`_scheduled_sync_loop`
  in `server.py`, added 2026-07-30) calls `127.0.0.1:8082` over loopback,
  so a squatter doesn't just break UI requests — it silently swallows the
  scheduled sync too.
- **Never use `GET /mcp` as a liveness probe.** Each such request makes
  FastMCP's streamable-HTTP manager open a transport session that is never
  closed — the old Dockerfile `HEALTHCHECK` did this every 30s (measured
  2026-09-29: +100 MiB RSS per 2000 probes). It also never failed, because
  `curl -s` without `-f` accepts `/mcp`'s 406. Use the lock-free
  `/healthz` route instead. As of 2026-09-29 that fix, plus a log cap in
  compose, is only on branch `opt/myfollows-20260929-0308` and isn't on
  master (leak and fix re-verified 2026-10-03; held because the
  `HEALTHCHECK` and log cap can only be proven by a real rebuild +
  redeploy; its PR #1 was then closed unmerged the same day, branch
  kept). Related: `server.py` binds `0.0.0.0` (LAN-reachable, no
  auth), even though the `docker-compose.yml` comment says it's
  127.0.0.1/host-local. Still open because the phone PWA may rely on LAN
  access.
- **Set `DATA_DIR` before importing `common.py` outside the container.**
  It defaults to `/data`, so an ad-hoc host-side import/benchmark silently
  creates a stray empty `/data/videos.db` on the host (happened 2026-09-29).
  Point it at a scratch copy, e.g. `DATA_DIR=/tmp/mfdata`, or run inside
  the image with the repo mounted.
- **YouTube login requires the host's X11 socket mounted in** (see
  `docker-compose.yml`'s `/tmp/.X11-unix` volume + `DISPLAY=:0`) — as of
  2026-07-29 this replaced an in-container Xvfb+VNC virtual display so the
  interactive Google login window pops up directly on the host desktop
  instead of through a noVNC tab. This only works because the container
  runs on the same machine as the desktop; it depends on that host's
  `xhost` already trusting local root connections (`SI:localuser:root`,
  the default on most desktop distros) — if the login window silently
  fails to appear, check `xhost` on the host first, not `youtube.py`.
  `docker compose up -d --force-recreate` is the more reliable option when
  debugging container state issues in general.
- **`youtube.py` caches its headless Playwright context across requests** —
  it only re-reads `data/youtube_storage_state.json` on first use after
  boot. `POST /api/youtube/reload_session` discards that cache so a freshly
  re-imported session takes effect without a container restart;
  `scripts/import_youtube_cookies.py` calls it automatically after writing
  the file.
- **Both cached browsers (`_get_context` in `server.py`,
  `_get_headless_context` in `youtube.py`) live for the whole process.**
  Before PR #5 (merged 2026-10-03, `55f433c`) they were never checked for
  liveness: if Chromium died while idle between the daily syncs, every
  later sync/login/backfill/play-URL refresh raised `TargetClosedError`
  until the container was restarted, and the scheduled sync only logged it
  (reproduced by SIGKILLing Chromium). Both now check `is_connected()` and
  relaunch; that doesn't cover the Playwright driver process itself dying.
- **Don't add Starlette's `GZipMiddleware`** — it would also wrap the
  `/api/play` video stream and the MCP transport. The large list responses
  are compressed per handler instead (`_json_gz` in `server.py`, PR #5,
  merged 2026-10-03; `/api/videos` 722 → 163 KB on the wire).
  `/api/videos` is unpaginated (~720 KB raw for 670 videos) and re-fetched
  on every page open and filter change.
- **`/api/play` shares one process-wide `httpx.AsyncClient`**
  (`_get_play_client` in `server.py`, PR #4, merged 2026-10-03) — a
  `<video>` sends a new Range request per seek, and building a client per
  request cost ~20 ms and a fresh upstream connection each time (measured
  23.2 → 3.2 ms, 205 → 1 connections). Don't go back to a per-request
  client; an unreachable upstream now returns 502 instead of a 500.
- **The idle browsers are the app's largest standing memory cost.**
  Measured 2026-10-07 (existing image, no network, blank pages, so a lower
  bound): 471 MiB PSS idle after one use each, of which the two Playwright
  node drivers are 230 MiB, the Chromiums 165 MiB and Python 76 MiB. PR #8
  (`opt/myfollows-20261007-0419`, open, not on master) closes each browser
  and its driver after `BROWSER_IDLE_CLOSE_SEC` (default 600, `0` = never)
  without use: 471 → 76 MiB, first use after a close ~370 ms instead of
  ~30 ms; not tested against a real sync. Reproduced by a review
  2026-10-07 (466 → 75 MiB) but held for two side effects: every idle
  close throws away cookies the site refreshed in the live context and
  the relaunch reloads the on-disk value (the storage-state files are
  written only at login), so that would happen ~10 min after each use
  instead of at a container restart, with an unknown effect on real
  logouts; and each close leaves 4 zombie `chrome-headless` processes,
  because Python is PID 1 in the container and doesn't reap them (none
  with `docker run --init`, so compose needs `init: true` before anything
  closes browsers at runtime). Not covered by it: the headed
  YouTube login browser is never closed after a login (`login_poll`/
  `login_wait` in `youtube.py` close only its context; read from the code,
  not reproduced).
- **`renderGrid` in `ui.html` builds every matching card at once and every
  filter change or search rebuilds them all.** Measured 2026-10-02 (headless Chromium, 4x CPU
  throttle, phone viewport, 670 cards): 1.8 s of main-thread work on page
  load, 1.2 s per re-render, and the search box re-rendered per keystroke.
  PR #6 (merged 2026-10-03, `3212c09`) added `content-visibility: auto` +
  `contain-intrinsic-size: auto 330px` on `.card` and a 150 ms search
  debounce (load 1.8 → 1.0 s, typing a 6-letter query 1.1 → 0.22 s, one
  render instead of six). With that, page height is an estimate until
  cards have rendered once (114,919 vs 120,865 px for 670 cards; no
  backward jumps in a headless wheel-scroll test); retune the `330px`
  placeholder if the card layout changes or the scrollbar jumps on a real
  phone, which is still untested.
- **The host is a desktop that suspends most nights, so timers must
  survive that.** `_scheduled_sync_loop` used to wait for `SYNC_HOUR` with
  one long `time.sleep()`, which counts monotonic time — frozen during
  suspend — so the sync fired late by the length of the suspend (hours
  after resume, not at resume). PR #6 (merged 2026-10-03, `3212c09`)
  replaced it with `_sleep_until` (re-checks the wall clock every 5 min,
  then waits 60 s for the network when a slot was missed); verified
  2026-10-02 and 2026-10-03 only against a simulated clock replaying the
  host's real suspend windows, not a real suspend — after a night slept
  through `SYNC_HOUR`, the log should show "Scheduled sync slot … missed
  by … min" about a minute after resume. Use wall-clock re-checks, not one long sleep, for
  any new timer here. A failed scheduled sync is still not retried until
  the next day's slot on master. PR #7 (`opt/myfollows-20261005-0506`)
  retries only a platform whose request raised or returned 5xx, after 2
  and 10 min, never a 200 carrying `{"error": ...}` (logged out, captcha);
  it was verified against a fake local server 2026-10-05 but is held: the
  owner has to decide whether those extra Douyin page loads are acceptable
  given its risk control.
- **A failed Douyin login start must not be retried back-to-back.** Each
  `/api/login/start` is a full Douyin page load in a fresh context. Until
  2026-10-08 `checkLogin()` in `ui.html` ran on a 2.5 s `setInterval` and
  called `startLogin()` whenever no login was in progress, so a failing
  start (captcha wall, stale selector) was repeated as fast as the server
  could load the page — 41 page loads in 2 min against a stub server with
  a 3 s start, up to 3 queued at once, and the error text was overwritten
  by "Loading QR…" before it could be read. Likewise `pollYoutubeLogin`
  polled `/api/youtube/status` every 2.5 s forever after an abandoned
  login window (1,440 requests per hour). Now: each poll is scheduled
  after the previous one answers, automatic start retries back off
  15 s → 10 min (`LOGIN_RETRY_*`, with a "Retry now" button), the YouTube
  poll gives up after 10 min (`YT_LOGIN_POLL_MAX_MS`), and neither polls
  while the tab is hidden (41 → 4 page loads in the same 2 min). Verified
  only against a stub server in headless Chromium, not a real captcha
  wall or a real phone. The backoff is per tab — two open tabs each retry
  on their own schedule; the server itself has no cooldown (it would also
  change the `douyin_login_start` MCP tool). On master since 2026-10-09
  (PR #9, `522ac25`; a review reproduced it, 47 → 4 with its own stub).
- **App source isn't bind-mounted — edits to `ui.html`/`server.py`/etc. need
  a rebuild to take effect.** `docker-compose.yml` only mounts `./data`; the
  Dockerfile `COPY`s `server.py common.py youtube.py ui.html` into the image
  at build time. Editing these files on disk does nothing to the running
  container. Confirmed 2026-08-07: a `ui.html` fix appeared not to work
  ("it still behaves the same way") purely because the container was still
  serving the pre-edit image. Always follow source edits with
  `docker compose build && docker compose up -d --force-recreate` before
  testing.
- **`ui.html` is a PWA (`apple-mobile-web-app-capable` + `viewport-fit=cover`
  in the viewport `<meta>` tag), so any new fixed/sticky top-anchored
  element draws under the phone's status bar (time/signal/battery) by
  default** — it must opt out itself with `padding-top:
  env(safe-area-inset-top)` (or `max(<existing-padding>, ...)` to keep a
  minimum). The video player overlay had this from the start; the sticky
  `header` and the off-canvas creator drawer didn't and were fixed
  2026-08-08. Check for this whenever adding a new top-anchored fixed/sticky
  panel.
- **`/api/status` and `/api/youtube/status` block for the full duration of
  any in-progress sync**, because they acquire the same global
  `asyncio.Lock`/`_headless_lock` (`server.py`/`youtube.py`) that
  `_run_sync`/the startup sync hold for their entire run — 60-90s+ is normal
  (confirmed 2026-08-09: 74s Douyin + 16s YouTube back-to-back on one boot).
  Historically `ui.html`'s `init()` awaited `checkLogin()` (which calls
  `/api/status`) before rendering anything, so opening the page during a
  sync stalled the whole grid for 35s+ even though `loadVideos()`/
  `loadCreators()` are plain local-DB reads with no lock involvement — the
  fix is having `init()` call `loadVideos()`/`loadCreators()` immediately
  with `checkLogin()` alongside, and having `checkLogin()` only reload them
  after a successful login poll. A 2026-08-09 version was never committed;
  the 2026-09-29 fix (20.2s → 0.4s with `/api/status` held 20s) was
  re-verified and merged to master 2026-10-03 (PR #3, `53385ca`). A side
  effect: while logged out, the grid from the local DB is already rendered
  behind the login overlay. Don't "fix" it
  server-side by letting `_login_poll_impl` skip the lock — that races
  `_login_start_impl` during relogin and can report a stale "logged in".
- **YouTube's embed iframe only loops a single video with `loop=1&playlist=<id>` together** —
  `loop=1` alone loops the surrounding "up next" queue instead of replaying
  the current video. Used as of 2026-09-18 in `ui.html`'s auto-repeat
  behavior for the playback page (the native Douyin `<video>` element just
  gets the plain `loop` attribute).
- **YouTube's embed iframe can silently substitute a "Sign in to confirm
  you're not a bot" interstitial for the real player**, with no client-side
  signal — it's a cross-origin page, so there's no `error` event to catch.
  This can't be fixed server-side (it's YouTube's bot detection on the
  viewer's IP). As of 2026-08-10, `ui.html`'s `armYtFallbackTimer` works
  around this by arming a 9s timer on every YouTube `openPlayer`/retry call
  and showing the existing fallback UI (Retry / "Open on YouTube") unless a
  postMessage arrives from the iframe first — the real player starts
  broadcasting almost immediately, the interstitial never does. If YouTube
  changes embed behavior (e.g. delays the first postMessage past 9s even on
  success), this heuristic will need retuning. Confirmed 2026-09-15: for one
  user this fired on nearly every video, tracked down (via asking whether
  they use a VPN/proxy) to a **full-tunnel VPN's exit IP** being flagged by
  YouTube's embed-only bot detection — the same IP loads `youtube.com`
  directly in a browser tab fine, because that stricter check applies only
  to anonymous iframe embeds. A client-side silent-auto-retry-before-fallback
  was tried and reverted (`ui.html`, briefly added and rolled back same
  session) since a flagged VPN exit IP fails identically on every retry;
  there is no code fix on our side for the VPN case, only split-tunneling
  `youtube.com` out of the VPN on the client.
- **The gated swipe/scroll navigation on the playback page (`ui.html`,
  added 2026-08-09) can silently fail on real phones in two distinct ways
  that don't reproduce in a desktop browser or simulated-touch testing —
  both confirmed and fixed 2026-08-10:**
  1. Over a YouTube video, touches land on the cross-origin
     `youtube.com/embed` iframe and are handled entirely inside YouTube's
     own document — they never reach the parent page's
     `touchstart`/`touchend`/`wheel` listeners on `#playerOverlay`, and
     there's no way to detect this client-side (no error, no event).
     Fixed with a transparent same-document `#playerYtCapture` div layered
     directly on top of `#playerFrame`, kept in sync with the iframe's
     show/hide state, which intercepts touches/wheel first and also
     handles tap-to-pause.
  2. Even without the iframe, a real phone's OS-level gesture recognizer
     can claim a vertical drag as a system gesture (pull-to-refresh,
     back-swipe) before it resolves to a `touchend` — when that happens the
     browser fires `touchcancel` instead, which nothing was listening for,
     so the swipe silently did nothing. Fixed with `touch-action: none` on
     `#playerOverlay` plus a `touchcancel` handler that resets swipe state.
     This class of bug is easy to miss because Playwright's simulated touch
     events don't trigger the OS gesture recognizer, so headless
     screenshot verification (this repo's usual test method for `ui.html`)
     won't catch it — real on-device testing is required for any future
     touch/swipe work here.
- **The live-platform "Unfollow"/"Unsubscribe" automation
  (`_unfollow_douyin_creator` in `server.py`, `youtube.unsubscribe`) is
  best-effort and known to fail silently on selector drift** — confirmed
  2026-09-18: Douyin's `button:has-text('已关注')` selector (flagged
  UNVERIFIED since 2026-07-29) couldn't find the follow button on a real
  profile page, so the live unfollow never happened even though the app
  reported the creator as removed. Because of this, `/api/videos/{id}/
  unfollow_creator` (and the `Unfollow`/`Unsubscribe` button in `ui.html`)
  no longer gate the local purge on the live action succeeding — it always
  deletes the creator's `creators` row and all their videos, and records
  them in a new `blocked_creators` table (`common.py`) that
  `upsert_creators`/`upsert_videos` consult so a later sync can't
  resurrect someone whose live unfollow silently failed. The response's
  `live_unfollowed` field tells the caller whether the real platform
  action was actually confirmed — if false, the user is still really
  following/subscribed on the live account and needs to unfollow there by
  hand until the selector is fixed. Fixing the Douyin selector itself
  needs a real followed profile page inspected with devtools, same as the
  original UNVERIFIED note asked for.
- **The sidebar creator list and the `#showUnlisted` checkbox are now
  linked** (as of 2026-09-18) — unlisted creators are hidden from
  `#creatorList` by default and only reappear when `#showUnlisted` is
  checked (`renderCreatorList` in `ui.html`), the same checkbox that
  already controlled whether unlisted creators' videos show in the grid.
  `/api/creators` itself still returns unlisted creators unconditionally
  (filtering is client-side), so don't mistake that endpoint's raw output
  for what the UI shows.
