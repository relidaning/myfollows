FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# xvfb/x11vnc/novnc/websockify/x11-utils back the YouTube login flow's
# in-browser VNC window (see start.sh, youtube.py) — not needed for Douyin.
# DEBIAN_FRONTEND=noninteractive is required here: novnc/x11vnc pull in
# tzdata as a transitive dependency, and tzdata's postinst otherwise blocks
# on an interactive geographic-area prompt with no TTY to answer it,
# hanging the build forever. TZ is already set via docker-compose.yml at
# runtime; this only affects package install-time debconf behavior.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl xvfb x11vnc novnc websockify x11-utils && \
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
