#!/bin/bash
# Share the local ops console with a temporary public HTTPS URL.
# Requires: cloudflared (brew install cloudflared) OR ngrok.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8501}"
echo "Sharing http://127.0.0.1:${PORT}"
echo "Keep the Streamlit (or FastAPI) console running in another terminal."
echo

if command -v cloudflared >/dev/null 2>&1; then
  exec cloudflared tunnel --url "http://127.0.0.1:${PORT}"
fi
if command -v ngrok >/dev/null 2>&1; then
  exec ngrok http "$PORT"
fi

echo "Install a tunnel first, then re-run:"
echo "  brew install cloudflared"
echo "  ./share.sh 8501          # Streamlit"
echo "  ./share.sh 8765          # FastAPI console"
exit 1
