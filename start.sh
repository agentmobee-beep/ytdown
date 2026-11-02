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

if [ -d venv ]; then
  GUNICORN=./venv/bin/gunicorn
  PYTHON=./venv/bin/python
else
  GUNICORN=gunicorn
  PYTHON=python3
fi

if command -v "$GUNICORN" >/dev/null 2>&1 || [ -x "$GUNICORN" ]; then
  exec "$GUNICORN" --bind 0.0.0.0:${PORT:-8000} --workers 1 --timeout 900 app:app
fi
exec "$PYTHON" app.py
