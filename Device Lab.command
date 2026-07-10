#!/bin/bash
cd "$(dirname "$0")" || exit 1
source .venv/bin/activate
pip install -q -r tools/requirements.txt

# Always replace any dashboard already bound to 8080 (stale code is a common cause of missing debug steps).
if command -v lsof >/dev/null 2>&1; then
  OLD_PIDS=$(lsof -ti tcp:8080 2>/dev/null || true)
  if [ -n "$OLD_PIDS" ]; then
    echo "Stopping previous process on port 8080 (PIDs: $OLD_PIDS)..."
    kill $OLD_PIDS 2>/dev/null || true
    sleep 1
    STILL=$(lsof -ti tcp:8080 2>/dev/null || true)
    if [ -n "$STILL" ]; then
      kill -9 $STILL 2>/dev/null || true
      sleep 1
    fi
  fi
fi

# Reload mid-debug-test can leave the server wedged; restart Device Lab after code changes.
export DASHBOARD_RELOAD=0

open "http://127.0.0.1:8080" 2>/dev/null &
exec python3 -m dashboard
