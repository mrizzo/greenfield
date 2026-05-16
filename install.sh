#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOADER="$SCRIPT_DIR/nightscout-dl.sh"
PLIST_ID="com.nightscout.downloader"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_ID}.plist"
LOG_DIR="$HOME/Library/Logs/nightscout-dl"

usage() {
    echo "Usage: $(basename "$0") [-c config] [uninstall]" >&2
    echo "  -c  path to config file (default: \$SCRIPT_DIR/config)" >&2
    exit 1
}

CONFIG="${NIGHTSCOUT_CONFIG:-$SCRIPT_DIR/config}"
while getopts ":c:h" opt; do
    case $opt in
        c) CONFIG="$OPTARG" ;;
        h) usage ;;
        :) echo "ERROR: -$OPTARG requires an argument" >&2; usage ;;
        \?) echo "ERROR: unknown option -$OPTARG" >&2; usage ;;
    esac
done
shift $((OPTIND - 1))

# ── Uninstall ─────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "uninstall" ]]; then
    echo "Uninstalling..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "Removed $PLIST_PATH and unloaded launchd job."
    exit 0
fi

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: config not found at $CONFIG" >&2; exit 1
fi
source "$CONFIG"

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ "${NIGHTSCOUT_URL:-}" == *"YOUR-SITE"* ]] || [[ -z "${NIGHTSCOUT_URL:-}" ]]; then
    echo "ERROR: set NIGHTSCOUT_URL in $CONFIG" >&2; exit 1
fi
if [[ -z "${NS_TOKEN:-}" ]] && [[ -z "${API_SECRET:-}" ]]; then
    echo "ERROR: set NS_TOKEN or API_SECRET in $CONFIG" >&2; exit 1
fi

chmod +x "$DOWNLOADER"

# ── Write plist ───────────────────────────────────────────────────────────────
RUN_HOUR="${RUN_HOUR:-1}"
RUN_MINUTE="${RUN_MINUTE:-0}"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

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
        <string>-c</string>
        <string>${CONFIG}</string>
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

echo "Installed. Runs daily at ${RUN_HOUR}:$(printf '%02d' "$RUN_MINUTE")."
echo "Logs: $LOG_DIR/"
echo ""
echo "To test right now:  bash $DOWNLOADER -c $CONFIG"
echo "To uninstall:       bash $SCRIPT_DIR/install.sh uninstall"
