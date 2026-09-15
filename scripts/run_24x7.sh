#!/bin/bash
set -euo pipefail
# Prefer the LaunchAgent copy outside Desktop (macOS TCC blocks Desktop executables).
SUPPORT_RUN="/Users/aritrachakraborty/Library/Application Support/com.polygrok.bot/run_24x7.sh"
if [[ -x "$SUPPORT_RUN" ]]; then
  exec "$SUPPORT_RUN"
fi
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec /usr/bin/caffeinate -ims -- "$REPO_DIR/.venv/bin/python" -m app.cli run
