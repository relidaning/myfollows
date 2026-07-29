FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# YouTube login now launches a headed Chromium straight onto the host's own
# X display (see docker-compose.yml's /tmp/.X11-unix mount + DISPLAY=:0, and
# youtube.py) instead of a virtual display + VNC — no Xvfb/x11vnc/novnc
# stack needed. Only curl (healthcheck) remains.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Base image already ships Chromium/Firefox/WebKit; this is a no-op unless
# the pinned playwright pip version drifts from the image's browser build.
RUN playwright install chromium --with-deps

COPY server.py common.py youtube.py ui.html .

EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -s --max-time 4 http://localhost:8082/mcp > /dev/null || exit 1

CMD ["python", "server.py"]
