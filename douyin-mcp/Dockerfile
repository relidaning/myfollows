FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# xvfb/x11vnc/novnc/websockify/x11-utils back the YouTube login flow's
# in-browser VNC window (see start.sh, youtube.py) — not needed for Douyin.
# matchbox-window-manager is required too, not optional: a bare Xvfb
# display has no window manager, which means X never transfers keyboard
# focus to a clicked-on window — confirmed 2026-07-28, the login page
# rendered and took mouse clicks over VNC, but every keystroke went
# nowhere. matchbox auto-maximizes the one window we ever show (the login
# browser), which also fixes focus. xdotool is a debugging aid only (lets
# `docker exec` simulate/verify real X input, unlike Playwright's CDP-based
# keyboard.type which bypasses X entirely and wouldn't have caught this).
# DEBIAN_FRONTEND=noninteractive is required here: novnc/x11vnc pull in
# tzdata as a transitive dependency, and tzdata's postinst otherwise blocks
# on an interactive geographic-area prompt with no TTY to answer it,
# hanging the build forever. TZ is already set via docker-compose.yml at
# runtime; this only affects package install-time debconf behavior.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl xvfb x11vnc novnc websockify x11-utils matchbox-window-manager xdotool && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Base image already ships Chromium/Firefox/WebKit; this is a no-op unless
# the pinned playwright pip version drifts from the image's browser build.
RUN playwright install chromium --with-deps

COPY server.py common.py youtube.py ui.html start.sh .
RUN chmod +x start.sh

EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -s --max-time 4 http://localhost:8082/mcp > /dev/null || exit 1

CMD ["./start.sh"]
