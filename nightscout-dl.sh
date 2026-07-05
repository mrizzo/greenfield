#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: $(basename "$0") [-c config] [-d YYYY-MM-DD]" >&2
    echo "  -c  path to config file (overrides \$NIGHTSCOUT_CONFIG env var)" >&2
    echo "  -d  date to download (default: yesterday)" >&2
    exit 1
}

TARGET_DATE=""
while getopts ":c:d:h" opt; do
    case $opt in
        c) NIGHTSCOUT_CONFIG="$OPTARG" ;;
        d) TARGET_DATE="$OPTARG" ;;
        h) usage ;;
        :) echo "ERROR: -$OPTARG requires an argument" >&2; usage ;;
        \?) echo "ERROR: unknown option -$OPTARG" >&2; usage ;;
    esac
done

CONFIG="${NIGHTSCOUT_CONFIG:-$SCRIPT_DIR/config}"

# ── Load config ──────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: config file not found at $CONFIG" >&2
    exit 1
fi
source "$CONFIG"

# ── Validate config ──────────────────────────────────────────────────────────
if [[ "$NIGHTSCOUT_URL" == *"YOUR-SITE"* ]]; then
    echo "ERROR: set NIGHTSCOUT_URL in $CONFIG" >&2; exit 1
fi
if [[ -z "${NS_TOKEN:-}" ]] && [[ -z "${API_SECRET:-}" ]]; then
    echo "ERROR: set NS_TOKEN or API_SECRET in $CONFIG" >&2; exit 1
fi

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required. Install with: brew install jq" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required." >&2; exit 1; }

# ── Auth ─────────────────────────────────────────────────────────────────────
if [[ -n "${NS_TOKEN:-}" ]]; then
    AUTH_PARAM="token=${NS_TOKEN}"
    AUTH_HEADER=""
else
    AUTH_PARAM=""
    AUTH_HEADER="api-secret: $(echo -n "$API_SECRET" | shasum -a 1 | awk '{print $1}')"
fi

# ── Dates ────────────────────────────────────────────────────────────────────
if [[ -n "$TARGET_DATE" ]]; then
    [[ "$TARGET_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
        || { echo "ERROR: date must be YYYY-MM-DD, got: $TARGET_DATE" >&2; exit 1; }
    YESTERDAY="$TARGET_DATE"
else
    YESTERDAY=$(date -v-1d +%Y-%m-%d)
fi
START_EPOCH=$(date -jf "%Y-%m-%d %H:%M:%S" "${YESTERDAY} 00:00:00" +%s)
END_EPOCH=$(date -jf "%Y-%m-%d %H:%M:%S" "${YESTERDAY} 23:59:59" +%s)
DAY_START_MS="${START_EPOCH}000"
DAY_END_MS="${END_EPOCH}999"
DAY_START_UTC=$(date -ujr "${START_EPOCH}" +"%Y-%m-%dT%H:%M:%S.000Z")
DAY_END_UTC=$(date -ujr "${END_EPOCH}" +"%Y-%m-%dT%H:%M:%S.999Z")

mkdir -p "$OUTPUT_DIR"

# ── Helper: fetch JSON from Nightscout ───────────────────────────────────────
ns_get() {
    local path="$1"
    local sep="?" ; [[ "$path" == *"?"* ]] && sep="&"
    local url="${NIGHTSCOUT_URL}${path}${AUTH_PARAM:+${sep}${AUTH_PARAM}}"
    local response http_code body

    response=$(curl -gs -w "\n__HTTP_CODE__:%{http_code}" \
        ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
        -H "Accept: application/json" \
        "$url" 2>&1) || { echo "ERROR: curl failed (exit $?): $response" >&2; return 1; }

    http_code="${response##*__HTTP_CODE__:}"
    body="${response%$'\n'__HTTP_CODE__:*}"

    if [[ "$http_code" != "200" ]]; then
        echo "ERROR: HTTP $http_code from $url" >&2
        echo "       Response: $body" >&2
        return 1
    fi

    echo "$body"
}

# ── CGM entries ──────────────────────────────────────────────────────────────
if [[ "${DOWNLOAD_ENTRIES:-1}" == "1" ]]; then
    OUT="$OUTPUT_DIR/${YESTERDAY}_entries.csv"
    echo "Downloading CGM entries for $YESTERDAY..."

    ns_get "/api/v1/entries.json?find[date][\$gte]=${DAY_START_MS}&find[date][\$lte]=${DAY_END_MS}&count=1440" \
    | jq -r '
        ["epoch_ms","sgv_mgdl","direction","noise","device"],
        (.[] | [
            (.mills // .date | floor),
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

    ns_get "/api/v1/treatments.json?find[created_at][\$gte]=${DAY_START_UTC}&find[created_at][\$lte]=${DAY_END_UTC}&count=9999" \
    | jq -r '
        ["epoch_ms","eventType","insulin","carbs","glucose","glucoseType","units","rate","duration","notes","enteredBy"],
        (.[] | [
            (.created_at | split(".")[0] + "Z" | fromdateiso8601 * 1000),
            (.eventType // ""),
            (.insulin // ""),
            (.carbs // ""),
            (.glucose // ""),
            (.glucoseType // ""),
            (.units // ""),
            (.rate // ""),
            (.duration // ""),
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

    ns_get "/api/v1/profile.json" > "$OUT"
    echo "  -> Profile saved to $OUT"
fi

# ── Analysis ─────────────────────────────────────────────────────────────────
# Run analyze.py for the day we just downloaded. A failure here shouldn't fail
# the whole run — the downloads are already saved — so warn instead of aborting.
if [[ "${RUN_ANALYSIS:-1}" == "1" ]]; then
    echo "Analyzing $YESTERDAY..."
    if command -v python3 >/dev/null 2>&1; then
        ANALYSIS_OUT="$OUTPUT_DIR/${YESTERDAY}_analysis.txt"
        # tee shows the analysis and saves it. pipefail (set at the top) makes
        # the pipeline fail if analyze.py fails even though tee succeeds, so a
        # failed run warns and removes the partial file instead of leaving it.
        if python3 "$SCRIPT_DIR/analyze.py" "$OUTPUT_DIR" -d "$YESTERDAY" | tee "$ANALYSIS_OUT"; then
            echo "  -> analysis saved to $ANALYSIS_OUT"
        else
            echo "WARNING: analysis failed (downloads are still saved)" >&2
            rm -f "$ANALYSIS_OUT"
        fi
    else
        echo "WARNING: python3 not found; skipping analysis" >&2
    fi
fi

echo "Done. Files are in $OUTPUT_DIR"
