#!/usr/bin/env bash
# Start Farm-checked phones via the dashboard API (always parallel).
#
# IMPORTANT: the LaunchAgent must run a COPY of this script from
#   ~/Library/Application Support/GramAddict/
# (not from Desktop — macOS blocks LaunchAgents from executing Desktop files).
# Use: tools/install_morning_start.sh
set -euo pipefail

# Prefer paths baked in by the installer; fall back for manual runs from the repo.
SUPPORT_DIR="${GRAMADDICT_SUPPORT_DIR:-$HOME/Library/Application Support/GramAddict}"
ROOT="${GRAMADDICT_ROOT:-}"

_is_project_root() {
  [[ -n "${1:-}" && -d "$1/accounts" && -d "$1/dashboard" ]]
}

# 1) Env from LaunchAgent
# 2) project_root.txt written by installer (needed when script lives in Application Support)
# 3) Parent of this script when run from the git repo's tools/
if ! _is_project_root "$ROOT"; then
  ROOT="$(cat "$SUPPORT_DIR/project_root.txt" 2>/dev/null || true)"
fi
if ! _is_project_root "$ROOT"; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd 2>/dev/null || true)"
fi
if ! _is_project_root "$ROOT"; then
  ROOT=""
fi

LOG_DIR="${SUPPORT_DIR}/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/morning_start.log"
DASHBOARD_URL="${DASHBOARD_URL:-http://127.0.0.1:8080}"

ts() { date "+%Y-%m-%d %I:%M:%S %p"; }

log() {
  echo "[$(ts)] $*" | tee -a "$LOG"
}

send_telegram() {
  local message="$1"
  python3 - "$SUPPORT_DIR" "$ROOT" "$message" <<'PY'
import json, sys, urllib.parse, urllib.request
from pathlib import Path

support, root, message = sys.argv[1], sys.argv[2], sys.argv[3]

def load_tg_from_dir(folder: Path):
    path = folder / "telegram.yml"
    if not path.is_file():
        return "", ""
    try:
        import yaml
    except ImportError:
        return "", ""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return str(data.get("telegram-api-token") or "").strip(), str(
        data.get("telegram-chat-id") or ""
    ).strip()

token = chat = ""
# Prefer credentials copied into Application Support (LaunchAgent-safe).
for name in ("615films", "yourlovefilms"):
    token, chat = load_tg_from_dir(Path(support) / "telegram" / name)
    if token and chat:
        break

# Fallback: project accounts/ (may fail under Desktop TCC — that's ok).
if (not token or not chat) and root:
    for name in ("615films", "yourlovefilms"):
        token, chat = load_tg_from_dir(Path(root) / "accounts" / name)
        if token and chat:
            break

if not token or not chat or "your-chat-id" in chat.lower():
    print("No telegram credentials — skip", file=sys.stderr)
    sys.exit(0)

url = f"https://api.telegram.org/bot{token}/sendMessage"
body = urllib.parse.urlencode(
    {"chat_id": chat, "text": message, "disable_web_page_preview": "true"}
).encode()
req = urllib.request.Request(url, data=body, method="POST")
with urllib.request.urlopen(req, timeout=20) as resp:
    data = json.loads(resp.read().decode())
if not data.get("ok"):
    raise SystemExit(f"Telegram error: {data}")
print("telegram ok")
PY
}

ensure_dashboard() {
  if curl -sf "$DASHBOARD_URL/api/gramaddict/accounts-status" >/dev/null 2>&1; then
    return 0
  fi
  log "Dashboard not reachable — asking Terminal to start it…"
  if [[ -z "$ROOT" || ! -d "$ROOT" ]]; then
    log "ERROR: project root unknown; cannot start dashboard."
    return 1
  fi
  # Terminal.app has Desktop access; LaunchAgent alone does not.
  local cmd
  cmd=$(printf 'cd %q && python3 -m dashboard' "$ROOT")
  osascript -e "tell application \"Terminal\" to do script \"$cmd\"" >/dev/null 2>&1 || true
  for _ in $(seq 1 45); do
    sleep 2
    if curl -sf "$DASHBOARD_URL/api/gramaddict/accounts-status" >/dev/null 2>&1; then
      log "Dashboard is up."
      return 0
    fi
  done
  return 1
}

log "Morning start begin (support=$SUPPORT_DIR root=${ROOT:-unknown})"

if ! ensure_dashboard; then
  log "ERROR: Dashboard still not reachable. Aborting."
  send_telegram "⚠️ Morning start failed — dashboard not running." || true
  exit 1
fi

selection_json="$(curl -sS "$DASHBOARD_URL/api/gramaddict/farm-selection" || echo '{}')"
log "Farm selection: $selection_json"

log "Launching Farm-checked phones in parallel…"
resp="$(curl -sS -X POST \
  "$DASHBOARD_URL/api/gramaddict/farm-run-selected" \
  -H "Content-Type: application/json" \
  -d '{"parallel": true}' || true)"
log "farm-run-selected → $resp"

eval "$(python3 - "$resp" <<'PY'
import json, shlex, sys
raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
try:
    data = json.loads(raw)
except Exception:
    data = {}
started = data.get("started") or []
failed = data.get("failed") or []
skipped = data.get("skipped") or []
serials = data.get("serials") or []
names = [f"@{s.get('username') or s.get('account_id')}" for s in started]
fail_names = [f"@{s.get('username') or s.get('account_id')}" for s in failed]
already = [
    f"@{s.get('username') or s.get('account_id')}"
    for s in skipped
    if "already running" in str(s.get("reason") or "").lower()
]
other_skip = [
    f"@{s.get('username') or s.get('account_id')}"
    for s in skipped
    if "already running" not in str(s.get("reason") or "").lower()
]
print(f"STARTED_COUNT={len(started)}")
print(f"FAILED_COUNT={len(failed)}")
print(f"SKIPPED_COUNT={len(skipped)}")
print(f"ALREADY_COUNT={len(already)}")
print(f"OTHER_SKIP_COUNT={len(other_skip)}")
print(f"SERIAL_COUNT={len(serials)}")
print("STARTED_NAMES=" + shlex.quote(" ".join(names)))
print("FAILED_NAMES=" + shlex.quote(" ".join(fail_names)))
print("ALREADY_NAMES=" + shlex.quote(" ".join(already)))
print("OTHER_SKIP_NAMES=" + shlex.quote(" ".join(other_skip)))
PY
)"

if [[ "${SERIAL_COUNT:-0}" -eq 0 ]]; then
  msg="⚠️ Morning — no Farm phones selected"
elif [[ "${STARTED_COUNT:-0}" -gt 0 && "${FAILED_COUNT:-0}" -eq 0 ]]; then
  if [[ "${ALREADY_COUNT:-0}" -gt 0 ]]; then
    msg="✅ Morning — ${STARTED_NAMES} · already running ${ALREADY_NAMES}"
  else
    msg="✅ Morning — ${STARTED_NAMES}"
  fi
elif [[ "${STARTED_COUNT:-0}" -gt 0 ]]; then
  msg="⚠️ Morning — ${STARTED_NAMES} · failed ${FAILED_NAMES}"
elif [[ "${ALREADY_COUNT:-0}" -gt 0 && "${FAILED_COUNT:-0}" -eq 0 ]]; then
  # Both (or all) were already running — not a failure.
  msg="✅ Morning — already running: ${ALREADY_NAMES}"
elif [[ "${FAILED_COUNT:-0}" -gt 0 ]]; then
  msg="❌ Morning — failed ${FAILED_NAMES}"
else
  msg="❌ Morning — nothing started"
fi

log "Telegram: $msg"
send_telegram "$msg" || log "Telegram send skipped/failed"
log "Morning start finished."
