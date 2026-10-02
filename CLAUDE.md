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
  compose, is only in PR #1 (`opt/myfollows-20260929-0308`) and isn't on
  master yet. Related: `server.py` binds `0.0.0.0` (LAN-reachable, no
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
  `_get_headless_context` in `youtube.py`) live for the whole process and,
  on master, are never checked for liveness.** If Chromium dies while idle
  between the daily syncs, every later sync/login/backfill/play-URL refresh
  raises `TargetClosedError` until the container is restarted, and the
  scheduled sync only logs it (reproduced 2026-10-02 by SIGKILLing
  Chromium). The `is_connected()`-then-relaunch fix is only in PR #5
  (`opt/myfollows-20261002-0331`), not master yet, and doesn't cover the
  Playwright driver process itself dying.
- **Don't add Starlette's `GZipMiddleware`** — it would also wrap the
  `/api/play` video stream and the MCP transport. PR #5 compresses the
  large list responses per handler instead (`_json_gz` in `server.py`).
  `/api/videos` is unpaginated (~720 KB for 670 videos) and re-fetched on
  every page open and filter change.
- **`renderGrid` in `ui.html` builds every matching card at once and every
  filter change or search rebuilds them all** — on master the search box
  does it per keystroke. Measured 2026-10-02 (headless Chromium, 4x CPU
  throttle, phone viewport, 670 cards): 1.8 s of main-thread work on page
  load, 1.2 s per re-render. PR #6 (`opt/myfollows-20261002-1200`, not on
  master yet) adds `content-visibility: auto` + `contain-intrinsic-size:
  auto 330px` on `.card` and a 150 ms search debounce (load 1.8 → 1.1 s,
  typing a 6-letter query 1.6 → 0.26 s). With that, page height is an
  estimate until cards have rendered once; retune the `330px` placeholder
  if the card layout changes or the scrollbar jumps on a real phone.
- **The host is a desktop that suspends most nights, and on master the
  daily sync doesn't survive that.** `_scheduled_sync_loop` waits for
  `SYNC_HOUR` with one long `time.sleep()`, which counts monotonic time —
  frozen during suspend — so the sync fires late by the length of the
  suspend (hours after resume, not at resume). PR #6 replaces it with
  `_sleep_until` (re-checks the wall clock every 5 min, then waits 60 s
  for the network when a slot was missed); verified 2026-10-02 only
  against a simulated clock replaying the host's real suspend windows,
  not a real suspend. Use wall-clock re-checks, not one long sleep, for
  any new timer here. A failed scheduled sync is still not retried until
  the next day's slot (open).
- **The login overlay polls with no backoff.** `checkLogin()` in `ui.html`
  runs every 2.5 s while logged out and calls `startLogin()` whenever no
  login is in progress, so a failing `/api/login/start` (captcha wall,
  stale selector) reopens a Douyin page on every poll. Likewise
  `pollYoutubeLogin` polls `/api/youtube/status` every 2.5 s until login
  succeeds, so an abandoned login window keeps the tab polling forever.
  Both read from the code 2026-10-02, not reproduced; still open.
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
  the 2026-09-29 fix (20.2s → 0.4s with `/api/status` held 20s) is only in
  PR #3 (`opt/myfollows-20260929-2050`), not master yet. Don't "fix" it
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
