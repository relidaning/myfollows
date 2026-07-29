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
  "flaky" bug here.
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
