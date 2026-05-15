#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/config"

# ── Load config ──────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: config file not found at $CONFIG" >&2
    exit 1
fi
source "$CONFIG"

# ── Validate config ──────────────────────────────────────────────────────────
if [[ "$NIGHTSCOUT_URL" == *"YOUR-SITE"* ]] || [[ "$API_SECRET" == "your-api-secret-here" ]]; then
    echo "ERROR: edit $CONFIG and set NIGHTSCOUT_URL and API_SECRET before running." >&2
    exit 1
fi

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required. Install with: brew install jq" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required." >&2; exit 1; }

# ── Auth header (SHA1 of API secret) ─────────────────────────────────────────
API_SECRET_HASH=$(echo -n "$API_SECRET" | shasum -a 1 | awk '{print $1}')

# ── Dates (yesterday) ────────────────────────────────────────────────────────
YESTERDAY=$(date -v-1d +%Y-%m-%d)
DAY_START="${YESTERDAY}T00:00:00.000Z"
DAY_END="${YESTERDAY}T23:59:59.999Z"

mkdir -p "$OUTPUT_DIR"

# ── Helper: fetch JSON from Nightscout ───────────────────────────────────────
ns_get() {
    local path="$1"
    curl -sf \
        -H "api-secret: $API_SECRET_HASH" \
        -H "Accept: application/json" \
        "${NIGHTSCOUT_URL}${path}"
}

# ── CGM entries ──────────────────────────────────────────────────────────────
if [[ "${DOWNLOAD_ENTRIES:-1}" == "1" ]]; then
    OUT="$OUTPUT_DIR/${YESTERDAY}_entries.csv"
    echo "Downloading CGM entries for $YESTERDAY..."

    ns_get "/api/v1/entries.json?find[dateString][\$gte]=${DAY_START}&find[dateString][\$lte]=${DAY_END}&count=1440" \
    | jq -r '
        ["date","time","sgv_mgdl","direction","noise","device"],
        (.[] | [
            (.dateString | split("T")[0]),
            (.dateString | split("T")[1] | split(".")[0]),
            (.sgv // ""),
            (.direction // ""),
            (.noise // ""),
            (.device // "")
        ])
        | @csv
    ' > "$OUT"

    COUNT=$(tail -n +2 "$OUT" | wc -l | tr -d ' ')
    echo "  -> $COUNT readings saved to $OUT"
fi

# ── Treatments ───────────────────────────────────────────────────────────────
if [[ "${DOWNLOAD_TREATMENTS:-1}" == "1" ]]; then
    OUT="$OUTPUT_DIR/${YESTERDAY}_treatments.csv"
    echo "Downloading treatments for $YESTERDAY..."

    ns_get "/api/v1/treatments.json?find[created_at][\$gte]=${DAY_START}&find[created_at][\$lte]=${DAY_END}&count=1000" \
    | jq -r '
        ["date","time","eventType","insulin","carbs","glucose","glucoseType","units","notes","enteredBy"],
        (.[] | [
            (.created_at | split("T")[0]),
            (.created_at | split("T")[1] | split(".")[0] | split("Z")[0]),
            (.eventType // ""),
            (.insulin // ""),
            (.carbs // ""),
            (.glucose // ""),
            (.glucoseType // ""),
            (.units // ""),
            (.notes // ""),
            (.enteredBy // "")
        ])
        | @csv
    ' > "$OUT"

    COUNT=$(tail -n +2 "$OUT" | wc -l | tr -d ' ')
    echo "  -> $COUNT treatments saved to $OUT"
fi

# ── Profile ───────────────────────────────────────────────────────────────────
if [[ "${DOWNLOAD_PROFILE:-1}" == "1" ]]; then
    OUT="$OUTPUT_DIR/${YESTERDAY}_profile.json"
    echo "Downloading profile..."

    # Profile is not time-series; save raw JSON (it rarely changes day-to-day).
    # Only write if it changed since yesterday's copy.
    PREV="$OUTPUT_DIR/$(date -v-2d +%Y-%m-%d)_profile.json"
    ns_get "/api/v1/profile.json" > "${OUT}.tmp"

    if [[ -f "$PREV" ]] && diff -q "$PREV" "${OUT}.tmp" >/dev/null 2>&1; then
        echo "  -> Profile unchanged since yesterday; skipping duplicate."
        rm "${OUT}.tmp"
    else
        mv "${OUT}.tmp" "$OUT"
        echo "  -> Profile saved to $OUT"
    fi
fi

echo "Done. Files are in $OUTPUT_DIR"
