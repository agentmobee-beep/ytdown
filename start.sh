#!/bin/sh
# Local / Render start helper.
cd "$(dirname "$0")"
if [ -d venv ]; then
  exec ./venv/bin/gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --timeout 900 app:app 2>/dev/null \
    || exec ./venv/bin/python app.py
else
  exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --timeout 900 app:app
fi
