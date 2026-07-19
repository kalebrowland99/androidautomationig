#!/usr/bin/env bash
# Install a macOS LaunchAgent that runs every day at 5:00 AM.
#
# Copies the starter OUT of Desktop into Application Support — LaunchAgents
# cannot execute scripts from Desktop (macOS privacy → exit 126).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_SCRIPT="$ROOT/tools/morning_start_bots.sh"
SUPPORT_DIR="$HOME/Library/Application Support/GramAddict"
INSTALLED_SCRIPT="$SUPPORT_DIR/morning_start_bots.sh"
PLIST_NAME="com.gramaddict.morningstart.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
LOG_DIR="$SUPPORT_DIR/logs"

mkdir -p "$SUPPORT_DIR/telegram" "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod +x "$SRC_SCRIPT"

# Copy runner to a LaunchAgent-safe location
cp "$SRC_SCRIPT" "$INSTALLED_SCRIPT"
chmod +x "$INSTALLED_SCRIPT"
echo "$ROOT" > "$SUPPORT_DIR/project_root.txt"

# Copy telegram creds so the agent can notify without reading Desktop
for account in 615films yourlovefilms; do
  src="$ROOT/accounts/$account/telegram.yml"
  if [[ -f "$src" ]]; then
    mkdir -p "$SUPPORT_DIR/telegram/$account"
    cp "$src" "$SUPPORT_DIR/telegram/$account/telegram.yml"
  fi
done

# Clear quarantine / provenance that can also trigger Operation not permitted
xattr -cr "$SUPPORT_DIR" 2>/dev/null || true

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.gramaddict.morningstart</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${INSTALLED_SCRIPT}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GRAMADDICT_SUPPORT_DIR</key>
    <string>${SUPPORT_DIR}</string>
    <key>GRAMADDICT_ROOT</key>
    <string>${ROOT}</string>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>5</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>WorkingDirectory</key>
  <string>${SUPPORT_DIR}</string>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/morning_start.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/morning_start.launchd.err.log</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.gramaddict.morningstart" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/com.gramaddict.morningstart" 2>/dev/null || true

# Schedule a wake a few minutes before 5 AM so sleep doesn't skip the job
if command -v pmset >/dev/null 2>&1; then
  # Best-effort; may require admin. Ignore failure.
  sudo pmset repeat wakeorpoweron MTWRFSU 04:55:00 2>/dev/null || true
fi

echo "✓ Reinstalled daily 5:00 AM starter (Desktop-safe)"
echo "  Runner: $INSTALLED_SCRIPT"
echo "  Plist:  $PLIST_PATH"
echo "  Logs:   $LOG_DIR/morning_start.log"
echo ""
echo "Why yesterday failed: LaunchAgent tried to run a script from Desktop and"
echo "macOS blocked it (exit 126 / Operation not permitted)."
echo ""
echo "Test now:"
echo "  \"$INSTALLED_SCRIPT\""
echo ""
echo "Or via launchd:"
echo "  launchctl kickstart -k gui/\$(id -u)/com.gramaddict.morningstart"
