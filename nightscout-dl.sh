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
# Remove a half-written .tmp if a download fails partway (the real file is left untouched).
TMP=""
trap 'rm -f "${TMP:-}" "$OUTPUT_DIR/${YESTERDAY}_profile.json.tmp"' EXIT

# ── Helper: fetch JSON from Nightscout ───────────────────────────────────────
ns_get() {
    local path="$1"
    local sep="?" ; [[ "$path" == *"?"* ]] && sep="&"
    local url="${NIGHTSCOUT_URL}${path}${AUTH_PARAM:+${sep}${AUTH_PARAM}}"
    local response http_code body

    # Up to 3 tries, 60s each: the server sometimes takes ~5s, and at 1am the Mac
    # may still be waking up with no network yet.
    local attempt
    for attempt in 1 2 3; do
        if response=$(curl -gs --connect-timeout 20 --max-time 60 -w "\n__HTTP_CODE__:%{http_code}" \
            ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
            -H "Accept: application/json" \
            "$url" 2>&1); then
            http_code="${response##*__HTTP_CODE__:}"
            body="${response%$'\n'__HTTP_CODE__:*}"
            if [[ "$http_code" == "200" ]]; then
                echo "$body"
                return 0
            fi
            echo "WARNING: HTTP $http_code (try $attempt/3): $body" >&2
        else
            echo "WARNING: curl failed (try $attempt/3)" >&2
        fi
        [[ $attempt -lt 3 ]] && sleep $((attempt * 30))
    done
    echo "ERROR: giving up on ${path%%\?*} after 3 tries" >&2
    return 1
}

SHORT_DAY=0

# ── CGM entries ──────────────────────────────────────────────────────────────
if [[ "${DOWNLOAD_ENTRIES:-1}" == "1" ]]; then
    OUT="$OUTPUT_DIR/${YESTERDAY}_entries.csv"; TMP="$OUT.tmp"
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
    ' > "$TMP"
    mv "$TMP" "$OUT"          # only reached if the download + jq succeeded (set -e, pipefail)

    COUNT=$(tail -n +2 "$OUT" | wc -l | tr -d ' ')
    echo "  -> $COUNT readings saved to $OUT"
    # A full day of 5-minute CGM readings is 288. Far fewer means a gap (sensor change,
    # phone offline, or a bad download) - say so loudly instead of silently succeeding.
    if (( COUNT < ${MIN_READINGS:-250} )); then
        echo "WARNING: only $COUNT readings for $YESTERDAY (expected ~288)" >&2
        osascript -e "display notification \"Only $COUNT CGM readings for $YESTERDAY (expected ~288)\" with title \"Nightscout download\"" 2>/dev/null || true
        SHORT_DAY=1
    fi
fi

# ── Treatments ───────────────────────────────────────────────────────────────
if [[ "${DOWNLOAD_TREATMENTS:-1}" == "1" ]]; then
    OUT="$OUTPUT_DIR/${YESTERDAY}_treatments.csv"; TMP="$OUT.tmp"
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
    ' > "$TMP"
    mv "$TMP" "$OUT"          # only reached if the download + jq succeeded (set -e, pipefail)

    COUNT=$(tail -n +2 "$OUT" | wc -l | tr -d ' ')
    echo "  -> $COUNT treatments saved to $OUT"
fi

# ── Profile ───────────────────────────────────────────────────────────────────
if [[ "${DOWNLOAD_PROFILE:-1}" == "1" ]]; then
    OUT="$OUTPUT_DIR/${YESTERDAY}_profile.json"
    echo "Downloading profile..."

    ns_get "/api/v1/profile.json" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
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

# ── Catch up on recent gaps ──────────────────────────────────────────────────
# On the nightly run, re-download any of the previous 7 days whose entries file
# is missing or empty (e.g. the Mac was asleep or offline at 1am that night).
if [[ -z "$TARGET_DATE" && "${CATCH_UP_DAYS:-7}" -gt 0 ]]; then
    for i in $(seq 2 "${CATCH_UP_DAYS:-7}"); do
        d=$(date -v-${i}d +%Y-%m-%d)
        f="$OUTPUT_DIR/${d}_entries.csv"
        n=$([[ -f "$f" ]] && tail -n +2 "$f" | wc -l | tr -d ' ' || echo 0)
        if (( n == 0 )); then             # missing/empty = failed download; short days are real gaps
            echo "Catching up $d (no data saved)..."
            bash "$0" ${NIGHTSCOUT_CONFIG:+-c "$NIGHTSCOUT_CONFIG"} -d "$d" || echo "WARNING: catch-up of $d failed" >&2
        fi
    done
fi

echo "Done. Files are in $OUTPUT_DIR"
[[ "$SHORT_DAY" == "0" ]] || exit 2      # launchd records a non-zero exit for short days
