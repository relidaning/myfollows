#!/bin/bash
# Boots a virtual X display + VNC server + noVNC web client so the YouTube
# login flow can hand the user a real, interactive Chromium window through
# the browser (Google blocks automated credential entry, so this has to be
# a human driving a real browser — see youtube.py's module docstring).
# Everything binds to 127.0.0.1 only: with docker-compose's
# network_mode: host, that keeps the VNC session host-local, never exposed
# to the LAN, even though it runs with no VNC password (-nopw).
set -e

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &

for i in $(seq 1 30); do
  if xdpyinfo -display :99 >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

x11vnc -display :99 -nopw -forever -shared -rfbport 5900 -listen 127.0.0.1 -bg -o /var/log/x11vnc.log
websockify --web=/usr/share/novnc 127.0.0.1:6082 127.0.0.1:5900 &

export DISPLAY=:99
exec python server.py
