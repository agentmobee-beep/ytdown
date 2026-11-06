#!/bin/sh
# YTDown start script (used locally and as the Docker container command).
#
# 1. If the bgutil PO-token server is available, start it in the background.
#    It answers YouTube "proof of origin" (PO token) requests for yt-dlp,
#    which helps extraction succeed from hosting-company IP addresses.
# 2. Start the web app with gunicorn.
cd "$(dirname "$0")"

# Where the bgutil server build lives. In the Docker image it is baked in at
# /opt/bgutil/server. Only set the default when the user has not set the
# variable at all - an explicitly set value (even an empty one, which
# disables the PO-token helpers) is always respected.
if [ -z "${YTDOWN_POT_SERVER_HOME+x}" ] && [ -d /opt/bgutil/server ]; then
  YTDOWN_POT_SERVER_HOME=/opt/bgutil/server
  export YTDOWN_POT_SERVER_HOME
fi

if [ -n "${YTDOWN_POT_SERVER_HOME:-}" ] \
   && [ -f "$YTDOWN_POT_SERVER_HOME/build/main.js" ] \
   && command -v node >/dev/null 2>&1; then
  echo "YTDown: starting bgutil PO-token server from $YTDOWN_POT_SERVER_HOME"
  node "$YTDOWN_POT_SERVER_HOME/build/main.js" &
else
  echo "YTDown: bgutil PO-token server not found - continuing without it."
  echo "YTDown: (extraction may fail on restricted hosts; see README-DEPLOY.md)"
fi

# WARP egress for YouTube: if a WARP profile exists (or can be
# registered now), run wireproxy as a SOCKS5 proxy on 127.0.0.1:25344.
# The app sends YouTube traffic - and only YouTube traffic - through
# it, because this host's own IP is blocked by YouTube.
if [ -d /opt/warp ]; then
  if [ ! -f /opt/warp/wgcf-profile.conf ] && [ -x /opt/warp/wgcf ]; then
    (cd /opt/warp && ./wgcf register --accept-tos >/dev/null 2>&1 \
      && ./wgcf generate -p wgcf-profile.conf >/dev/null 2>&1) || true
  fi
  if [ -f /opt/warp/wgcf-profile.conf ]; then
    grep -q '^\[Socks5\]' /opt/warp/wgcf-profile.conf \
      || printf '\n[Socks5]\nBindAddress = 127.0.0.1:25344\n' >> /opt/warp/wgcf-profile.conf
    if command -v wireproxy >/dev/null 2>&1; then
      echo "YTDown: starting WARP proxy (wireproxy) on 127.0.0.1:25344"
      wireproxy -c /opt/warp/wgcf-profile.conf &
      sleep 2
    fi
  else
    echo "YTDown: no WARP profile available - YouTube will use the direct egress."
  fi
fi

if [ -d venv ]; then
  GUNICORN=./venv/bin/gunicorn
  PYTHON=./venv/bin/python
else
  GUNICORN=gunicorn
  PYTHON=python3
fi

if command -v "$GUNICORN" >/dev/null 2>&1 || [ -x "$GUNICORN" ]; then
  exec "$GUNICORN" --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 4 --timeout 900 app:app
fi
exec "$PYTHON" app.py
