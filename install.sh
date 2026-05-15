#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config"
DOWNLOADER="$SCRIPT_DIR/nightscout-dl.sh"
PLIST_ID="com.nightscout.downloader"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_ID}.plist"
LOG_DIR="$HOME/Library/Logs/nightscout-dl"

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ "$1" == "uninstall" ]]; then
    echo "Uninstalling..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "Removed $PLIST_PATH and unloaded launchd job."
    exit 0
fi

if [[ "$NIGHTSCOUT_URL" == *"YOUR-SITE"* ]] 2>/dev/null || ! grep -q "NIGHTSCOUT_URL" "$CONFIG"; then
    source "$CONFIG"
fi
source "$CONFIG"

if [[ "$NIGHTSCOUT_URL" == *"YOUR-SITE"* ]] || [[ "$API_SECRET" == "your-api-secret-here" ]]; then
    echo "ERROR: fill in NIGHTSCOUT_URL and API_SECRET in $CONFIG before installing." >&2
    exit 1
fi

chmod +x "$DOWNLOADER"

# ── Write plist ───────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_ID}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${DOWNLOADER}</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${RUN_HOUR}</integer>
        <key>Minute</key>
        <integer>${RUN_MINUTE}</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/nightscout-dl.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/nightscout-dl.err</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST

# ── Load (or reload) ──────────────────────────────────────────────────────────
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "Installed. The script will run daily at ${RUN_HOUR}:$(printf '%02d' "$RUN_MINUTE")."
echo "Logs: $LOG_DIR/"
echo ""
echo "To test right now:  bash $DOWNLOADER"
echo "To uninstall:       bash $SCRIPT_DIR/install.sh uninstall"
