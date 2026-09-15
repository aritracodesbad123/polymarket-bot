#!/bin/bash
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
if [ ! -x .venv/bin/uvicorn ]; then
  /usr/bin/python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8765 --log-level warning
