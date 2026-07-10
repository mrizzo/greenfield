#!/usr/bin/env python3
"""Tool functions Greenfield (greenfield.py) exposes to Claude, plus the
code-enforced guardrails.

Everything here reuses the existing repo code rather than reimplementing it:
downloads go through nightscout-dl.sh, and all parsing / the median-TDD
calculation come from analyze.py. This module is strictly read-only with
respect to any pump/Trio/Nightscout configuration — it only reads exported
CSV/JSON and appends to proposals.jsonl.
"""

import glob
import json
import os
import statistics
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import analyze as az  # reuse the existing loaders + median-TDD calc

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NIGHTSCOUT_DL = os.path.join(SCRIPT_DIR, "nightscout-dl.sh")
PROPOSALS_LOG = os.path.join(SCRIPT_DIR, "proposals.jsonl")
OUTPUT_DIR = os.path.expanduser(
    os.environ.get("NIGHTSCOUT_OUTPUT_DIR", "~/nightscout-exports")
)

# ── Guardrail constants (enforced in code, not in the prompt) ────────────────
MIN_DAYS = 10               # refuse with fewer than this many days of data
MAX_GAP_FRACTION = 0.20     # refuse if >20% of expected CGM readings are missing
MAX_CHANGE_FRACTION = 0.15  # >15% change must be marked large + justified
ISF_RULE = 1800             # Rule of 1800 (mg/dL): ISF ≈ 1800 / TDD
CARB_RULE = 500             # 500 rule: carb ratio ≈ 500 / TDD
SANITY_DIVERGENCE = 0.30    # flag (don't block) if data number diverges > this
GAP_THRESHOLD_MIN = 30      # a CGM gap is > this many minutes
CGM_CADENCE_MIN = 5         # expected reading interval


# ── Loading helpers ──────────────────────────────────────────────────────────
def _load_window(days):
    """Load entries/treatments/profiles for the most recent `days` days present
    in OUTPUT_DIR, with scheduled basal filled in (mirrors analyze.main)."""
    profiles, tz = az.load_profiles(OUTPUT_DIR)
    entries = az.load_entries(OUTPUT_DIR, tz=tz)
    treatments = az.load_treatments(OUTPUT_DIR, tz=tz)

    if entries and days and days > 0:
        all_days = sorted({e["dt"].date() for e in entries})
        cutoff = all_days[-min(days, len(all_days))]
        entries = [e for e in entries if e["dt"].date() >= cutoff]
        treatments = [t for t in treatments if t["dt"].date() >= cutoff]

    if profiles and entries:
        dates = sorted({e["dt"].date() for e in entries})
        treatments = az.add_scheduled_basal(treatments, profiles, dates)

    return entries, treatments, profiles, tz


def _schedule(entries_list):
    """[{time,value}] -> [{"time": "HH:MM", "value": v}] with dates as strings."""
    return [{"time": e.get("time"), "value": e.get("value")} for e in entries_list]


# ── Data-sufficiency gate (used by the hard guardrail in greenfield.py) ──────
def data_sufficiency(days):
    """Return coverage stats + whether the window clears the MIN_DAYS / gap
    thresholds. Pure measurement — the refuse/stop decision lives in the loop."""
    entries, _, _, _ = _load_window(days)
    if not entries:
        return {
            "days_available": 0,
            "actual_readings": 0,
            "expected_readings": 0,
            "gap_fraction": 1.0,
            "ok": False,
            "reasons": ["no CGM data found — run fetch_recent_data first"],
        }

    day_set = {e["dt"].date() for e in entries}
    days_available = len(day_set)
    span_min = (entries[-1]["dt"] - entries[0]["dt"]).total_seconds() / 60
    expected = max(1, int(span_min / CGM_CADENCE_MIN))
    actual = len(entries)
    gap_fraction = max(0.0, 1.0 - actual / expected)

    reasons = []
    if days_available < MIN_DAYS:
        reasons.append(f"only {days_available} days of data (need >= {MIN_DAYS})")
    if gap_fraction > MAX_GAP_FRACTION:
        reasons.append(
            f"CGM gap coverage {gap_fraction:.0%} exceeds {MAX_GAP_FRACTION:.0%}"
        )

    return {
        "days_available": days_available,
        "actual_readings": actual,
        "expected_readings": expected,
        "gap_fraction": round(gap_fraction, 3),
        "ok": not reasons,
        "reasons": reasons,
    }


# ── Tools exposed to Claude ──────────────────────────────────────────────────
def fetch_recent_data(days):
    """Download the last `days` days via nightscout-dl.sh (RUN_ANALYSIS=0 so it
    only downloads). Reuses the existing downloader; no curl/jq reimplementation."""
    if not os.path.isfile(NIGHTSCOUT_DL):
        return {"error": f"nightscout-dl.sh not found at {NIGHTSCOUT_DL}"}

    env = {**os.environ, "RUN_ANALYSIS": "0"}
    today = date.today()
    results = []
    for i in range(1, int(days) + 1):
        d = (today - timedelta(days=i)).isoformat()
        try:
            proc = subprocess.run(
                ["bash", NIGHTSCOUT_DL, "-d", d],
                env=env, capture_output=True, text=True, timeout=180,
            )
            entries_file = os.path.join(OUTPUT_DIR, f"{d}_entries.csv")
            n = 0
            if os.path.isfile(entries_file):
                with open(entries_file) as fh:
                    n = max(0, sum(1 for _ in fh) - 1)  # minus header
            results.append({"date": d, "ok": proc.returncode == 0, "readings": n,
                            "error": None if proc.returncode == 0 else proc.stderr[-300:]})
        except subprocess.TimeoutExpired:
            results.append({"date": d, "ok": False, "readings": 0, "error": "timeout"})

    return {"requested_days": int(days), "output_dir": OUTPUT_DIR, "downloaded": results}


def get_tdd_series(days):
    """Per-day total daily dose plus the trailing median over the window.
    Reuses analyze.tdd_stats (the median-TDD calculation)."""
    _, treatments, _, _ = _load_window(days)
    if not treatments:
        return {"days": [], "trailing_median_tdd": None,
                "note": "no treatment data in window"}

    per_day = []
    for d in sorted({t["dt"].date() for t in treatments}):
        day_tx = [t for t in treatments if t["dt"].date() == d]
        s = az.tdd_stats(day_tx)  # single day -> that day's totals
        if s:
            per_day.append({
                "date": d.isoformat(),
                "tdd": round(s["median_tdd"], 1),
                "bolus": round(s["bolus"], 1),
                "basal": round(s["total_basal"], 1),
            })

    trailing = az.tdd_stats(treatments)  # median across the window
    return {
        "days": per_day,
        "trailing_median_tdd": round(trailing["median_tdd"], 1) if trailing else None,
        "tdd_range": [round(x, 1) for x in trailing["tdd_range"]] if trailing else None,
    }


def get_basal_segments(target_date):
    """Hourly bolus/basal delivery for a single day (YYYY-MM-DD).
    Reuses analyze.hourly_insulin after filtering to that day."""
    try:
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return {"error": f"date must be YYYY-MM-DD, got: {target_date}"}

    _, treatments, _, _ = _load_window(0)  # load all, scheduled basal filled
    day_tx = [t for t in treatments if t["dt"].date() == d]
    if not day_tx:
        return {"date": target_date, "hours": [], "note": "no treatments for this day"}

    ins = az.hourly_insulin(day_tx)  # {hour: (bolus, basal)}
    hours = [
        {"hour": h, "bolus": round(b, 2), "basal": round(bl, 2),
         "total": round(b + bl, 2)}
        for h, (b, bl) in sorted(ins.items())
    ]
    return {"date": target_date, "hours": hours}


def find_cgm_gaps(days):
    """CGM gaps > 30 min in the window, cross-referenced against pump-suspend
    treatments when that data is present."""
    entries, treatments, _, _ = _load_window(days)
    if not entries:
        return {"days": int(days), "gaps": [], "note": "no CGM data in window"}

    suspends = [t for t in treatments
                if "suspend" in (t.get("type") or "").lower()]
    suspend_available = bool(suspends)

    gaps = []
    for a, b in zip(entries, entries[1:]):
        gap_min = (b["dt"] - a["dt"]).total_seconds() / 60
        if gap_min <= GAP_THRESHOLD_MIN:
            continue
        overlapping = [s["dt"].isoformat() for s in suspends
                       if a["dt"] <= s["dt"] <= b["dt"]]
        gaps.append({
            "start": a["dt"].isoformat(),
            "end": b["dt"].isoformat(),
            "minutes": round(gap_min),
            "explained_by_suspend": bool(overlapping),
            "suspend_events": overlapping,
        })

    return {
        "days": int(days),
        "threshold_min": GAP_THRESHOLD_MIN,
        "suspend_data_available": suspend_available,
        "gap_count": len(gaps),
        "gaps": gaps,
    }


def _block_label(block):
    return f"{block:02d}:00-{block + 2:02d}:00"


def get_fasting_drift(days):
    """BG drift by 2-hour block during fasting periods (no insulin/carbs in the
    prior 4h). Positive = BG rising = basal likely too low; negative = too high.
    Reuses analyze.fasting_bg_trends — the primary basal-adjustment signal."""
    entries, treatments, _, _ = _load_window(days)
    if not entries:
        return {"days": int(days), "blocks": [], "note": "no CGM data in window"}

    trends = az.fasting_bg_trends(entries, treatments)  # {block:int -> mg/dL/hr}
    blocks = []
    for b in sorted(trends):
        rate = trends[b]
        if rate > 3:
            direction = "rising (basal likely too low)"
        elif rate < -3:
            direction = "falling (basal likely too high)"
        else:
            direction = "stable"
        blocks.append({"time_block": _block_label(b),
                       "mg_dl_per_hr": round(rate, 1), "direction": direction})

    return {
        "days": int(days),
        "note": "fasting periods only (>=10 samples per block); positive = need "
                "more basal, negative = need less",
        "blocks": blocks,
    }


def get_observed_isf(days):
    """Empirically observed ISF by 2-hour block, from real correction boluses
    with no carbs within ±2h. Reuses analyze.empirical_isf — the primary
    ISF-adjustment signal."""
    entries, treatments, _, _ = _load_window(days)
    if not entries:
        return {"days": int(days), "by_block": [], "overall_isf": None,
                "note": "no CGM data in window"}

    (overall_isf, n_overall), by_block = az.empirical_isf(entries, treatments)
    blocks = [
        {"time_block": _block_label(b), "isf_mg_dl_per_u": round(v), "n": n}
        for b, (v, n) in sorted(by_block.items()) if n >= 2
    ]
    return {
        "days": int(days),
        "overall_isf": round(overall_isf) if overall_isf else None,
        "overall_n": n_overall,
        "by_block": blocks,
        "note": "observed ISF = (BG at correction - nadir over next 3h) / units; "
                "higher n = more confidence",
    }


def get_hourly_bg(days):
    """Average CGM by hour of day over the window (analyze.hourly_avg_bg).
    Works on closed-loop data where fasting-drift / observed-ISF are empty —
    persistent highs/lows in a time block point at basal/ISF for that block."""
    entries, _, _, _ = _load_window(days)
    if not entries:
        return {"days": int(days), "hours": [], "note": "no CGM data in window"}

    h = az.hourly_avg_bg(entries)
    hours = []
    for hr in sorted(h):
        avg = h[hr]
        flag = "high" if avg > 180 else ("low" if avg < 80 else "in-range")
        hours.append({"hour": hr, "avg_mg_dl": round(avg), "flag": flag})

    return {
        "days": int(days),
        "note": "mean CGM by hour of day; a block that runs persistently high or "
                "low is a candidate for a basal/ISF change in that block",
        "hours": hours,
    }


def get_current_settings():
    """Current basal / ISF / carb-ratio profile, read from the newest downloaded
    *_profile.json (what DOWNLOAD_PROFILE fetches from Nightscout). Read-only."""
    files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "*_profile.json")))
    if not files:
        return {"error": "no *_profile.json found — enable DOWNLOAD_PROFILE and "
                         "run fetch_recent_data"}
    path = files[-1]
    try:
        data = json.load(open(path))
        store = data[0]["store"]["default"]
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        return {"error": f"could not parse {os.path.basename(path)}: {e}"}

    def val(schedule):  # first value when there's a single all-day entry
        return schedule[0]["value"] if len(schedule) == 1 else None

    return {
        "source_file": os.path.basename(path),
        "profile_date": os.path.basename(path)[:10],
        "units": store.get("units"),
        "timezone": store.get("timezone"),
        "dia_hours": store.get("dia"),
        "basal": _schedule(store.get("basal", [])),
        "isf": _schedule(store.get("sens", [])),          # sens == ISF
        "carb_ratio": _schedule(store.get("carbratio", [])),
        "carb_ratio_all_day": val(store.get("carbratio", [])),
        "target_low": val(store.get("target_low", [])),
        "target_high": val(store.get("target_high", [])),
        "note": "read-only snapshot; Greenfield never writes to the profile",
    }


# ── Guardrail: validate a proposal (runs in code after Claude proposes) ──────
def validate_proposal(proposal, tdd=None):
    """Reject changes with no citations and unmarked >15% changes; flag (don't
    block) numbers that diverge from the Rule-of-1800 / 500-rule sanity floors."""
    rejections = []
    flags = []
    changes = proposal.get("changes") or []

    if not proposal.get("no_change") and not changes:
        rejections.append("no changes provided and no_change is not set")

    for i, ch in enumerate(changes):
        label = f"{ch.get('setting','?')}@{ch.get('time_block','?')}"

        citations = ch.get("citations") or []
        if not citations:
            rejections.append(f"change #{i+1} ({label}) has no data citations")

        cur = ch.get("current_value")
        prop = ch.get("proposed_value")
        if isinstance(cur, (int, float)) and isinstance(prop, (int, float)) and cur:
            frac = abs(prop - cur) / abs(cur)
            if frac > MAX_CHANGE_FRACTION:
                marked = ch.get("large_change") and (ch.get("justification") or "").strip()
                if not marked:
                    rejections.append(
                        f"change #{i+1} ({label}) is {frac:.0%} (> "
                        f"{MAX_CHANGE_FRACTION:.0%}); mark large_change + justification"
                    )

        # Sanity floors (flags only)
        if tdd and isinstance(prop, (int, float)) and prop:
            if ch.get("setting") == "isf":
                rule = ISF_RULE / tdd
                if abs(prop - rule) / rule > SANITY_DIVERGENCE:
                    flags.append(
                        f"{label}: proposed ISF {prop:.0f} diverges from Rule-of-1800 "
                        f"estimate {rule:.0f} mg/dL/U (TDD {tdd:.1f})"
                    )
            elif ch.get("setting") == "carb_ratio":
                rule = CARB_RULE / tdd
                if abs(prop - rule) / rule > SANITY_DIVERGENCE:
                    flags.append(
                        f"{label}: proposed carb ratio {prop:.0f} diverges from 500-rule "
                        f"estimate {rule:.0f} g/U (TDD {tdd:.1f})"
                    )

    return {"ok": not rejections, "rejections": rejections, "flags": flags}


# ── log_proposal — append-only audit log (must fire on every run) ────────────
def log_proposal(proposal, validation=None, refusal_reason=None, meta=None):
    """Append the full proposal + reasoning + validation result to
    proposals.jsonl (created if missing)."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "refusal_reason": refusal_reason,
        "proposal": proposal,
        "validation": validation,
        "meta": meta or {},
    }
    with open(PROPOSALS_LOG, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    return {"logged": True, "path": PROPOSALS_LOG, "timestamp": record["timestamp"]}
