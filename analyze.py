#!/usr/bin/env python3
"""Suggest ISF and basal rates from Nightscout CSV exports."""

import argparse
import csv
import glob
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta


def parse_dt(date_str, time_str):
    try:
        return datetime.fromisoformat(f"{date_str}T{time_str}")
    except ValueError:
        return None


def load_entries(directory):
    rows = []
    for f in sorted(glob.glob(os.path.join(directory, "*_entries.csv"))):
        with open(f, newline="") as fh:
            for row in csv.DictReader(fh):
                if not row.get("sgv_mgdl"):
                    continue
                dt = parse_dt(row["date"], row["time"])
                if dt:
                    try:
                        rows.append({"dt": dt, "sgv": int(row["sgv_mgdl"])})
                    except ValueError:
                        pass
    return sorted(rows, key=lambda r: r["dt"])


def load_treatments(directory):
    rows = []
    for f in sorted(glob.glob(os.path.join(directory, "*_treatments.csv"))):
        with open(f, newline="") as fh:
            for row in csv.DictReader(fh):
                dt = parse_dt(row["date"], row["time"])
                if not dt:
                    continue
                rate = float(row["rate"]) if row.get("rate") else 0.0
                duration = float(row["duration"]) if row.get("duration") else 0.0
                # For temp basals, insulin delivered = rate (U/hr) * duration (min) / 60
                basal_delivered = rate * duration / 60 if rate and duration else 0.0
                rows.append({
                    "dt": dt,
                    "type": row.get("eventType", ""),
                    "insulin": float(row["insulin"]) if row.get("insulin") else basal_delivered,
                    "carbs": float(row["carbs"]) if row.get("carbs") else 0.0,
                    "rate": rate,
                    "duration": duration,
                })
    return sorted(rows, key=lambda r: r["dt"])


def tdd_stats(treatments):
    by_day = defaultdict(float)
    for t in treatments:
        if t["insulin"] > 0:
            by_day[t["dt"].date()] += t["insulin"]
    if not by_day:
        return None, []
    values = list(by_day.values())
    return statistics.mean(values), values


def empirical_isf(entries, treatments):
    """
    Correction boluses with no carbs within ±2h.
    ISF = (BG at injection - nadir over next 3h) / units.
    """
    sgv_index = {e["dt"]: e["sgv"] for e in entries}

    def nearest_sgv(target, window=10):
        for delta in range(0, window + 1):
            for sign in (1, -1):
                v = sgv_index.get(target + timedelta(minutes=sign * delta))
                if v is not None:
                    return v
        return None

    results = []
    for i, t in enumerate(treatments):
        if t["insulin"] <= 0:
            continue
        nearby_carbs = any(
            abs((o["dt"] - t["dt"]).total_seconds()) < 7200 and o["carbs"] > 0
            for o in treatments if o is not t
        )
        if nearby_carbs:
            continue

        sgv_start = nearest_sgv(t["dt"])
        nadir = None
        for mins in range(30, 181, 5):
            v = nearest_sgv(t["dt"] + timedelta(minutes=mins))
            if v is not None and (nadir is None or v < nadir):
                nadir = v

        if sgv_start and nadir and sgv_start > nadir:
            observed = (sgv_start - nadir) / t["insulin"]
            if 10 < observed < 250:
                results.append(observed)

    if results:
        return statistics.median(results), len(results)
    return None, 0


def fasting_bg_trends(entries, treatments):
    """
    For each consecutive CGM pair with no insulin/carbs in the prior 4h,
    compute BG rate of change. Group into 2-hour blocks.
    Positive = BG rising (basal too low), negative = falling (basal too high).
    """
    block_rates = defaultdict(list)

    for i in range(len(entries) - 1):
        a, b = entries[i], entries[i + 1]
        gap_min = (b["dt"] - a["dt"]).total_seconds() / 60
        if gap_min < 1 or gap_min > 10:
            continue

        window_start = a["dt"] - timedelta(hours=4)
        if any(
            window_start <= t["dt"] <= b["dt"] and (t["insulin"] > 0 or t["carbs"] > 0)
            for t in treatments
        ):
            continue

        rate = (b["sgv"] - a["sgv"]) / (gap_min / 60)  # mg/dL per hour
        block = (a["dt"].hour // 2) * 2
        block_rates[block].append(rate)

    return {
        block: statistics.median(rates)
        for block, rates in block_rates.items()
        if len(rates) >= 10
    }


def hourly_avg_bg(entries):
    by_hour = defaultdict(list)
    for e in entries:
        by_hour[e["dt"].hour].append(e["sgv"])
    return {h: statistics.mean(v) for h, v in by_hour.items()}


def bar(value, scale=10, width=20):
    filled = min(int(abs(value) / scale), width)
    return "█" * filled


def main():
    parser = argparse.ArgumentParser(description="Suggest ISF and basal rates from Nightscout exports")
    parser.add_argument("directory", nargs="?",
                        default=os.path.expanduser("~/nightscout-exports"),
                        help="Directory containing exported CSVs (default: ~/nightscout-exports)")
    args = parser.parse_args()

    print(f"Analyzing: {args.directory}\n")

    entries = load_entries(args.directory)
    treatments = load_treatments(args.directory)

    if not entries:
        print("No entries found. Run nightscout-dl.sh first.")
        return

    days = len({e["dt"].date() for e in entries})
    date_range = f"{entries[0]['dt'].date()} → {entries[-1]['dt'].date()}"
    print(f"  {days} days  |  {date_range}  |  {len(entries)} readings  |  {len(treatments)} treatments\n")

    # ── TDD & ISF ─────────────────────────────────────────────────────────────
    avg_tdd, tdd_values = tdd_stats(treatments)
    print("── Insulin ──────────────────────────────────────────────────────────")
    if avg_tdd:
        isf_rule = 1700 / avg_tdd
        tdd_min, tdd_max = min(tdd_values), max(tdd_values)
        print(f"  Avg TDD:          {avg_tdd:.1f} U/day  (range {tdd_min:.1f}–{tdd_max:.1f})")
        print(f"  ISF (1700 rule):  {isf_rule:.0f} mg/dL per unit")
    else:
        print("  No insulin data found.")

    emp_isf, n_corrections = empirical_isf(entries, treatments)
    if emp_isf:
        print(f"  ISF (observed):   {emp_isf:.0f} mg/dL per unit  ({n_corrections} corrections analysed)")

    # ── Basal estimate ────────────────────────────────────────────────────────
    print()
    print("── Basal estimate ───────────────────────────────────────────────────")
    if avg_tdd:
        total_basal = avg_tdd * 0.45
        flat_rate = total_basal / 24
        print(f"  Total basal/day:  ~{total_basal:.1f} U  (45% of TDD)")
        print(f"  Flat rate:        ~{flat_rate:.2f} U/hr")

    # ── Fasting drift by time block ───────────────────────────────────────────
    trends = fasting_bg_trends(entries, treatments)
    if trends:
        print()
        print("── Basal direction by time (fasting periods only) ───────────────────")
        print("  ↑ BG rising = need more basal   ↓ BG falling = need less")
        print()
        for block in sorted(trends):
            rate = trends[block]
            if rate > 3:
                verdict = "↑ too low "
            elif rate < -3:
                verdict = "↓ too high"
            else:
                verdict = "✓ ok      "
            direction = "+" if rate >= 0 else ""
            print(f"  {block:02d}:00–{block+2:02d}:00  {direction}{rate:+.1f} mg/dL/hr  {verdict}  {bar(rate, scale=5)}")

    # ── Hourly average BG ─────────────────────────────────────────────────────
    hourly = hourly_avg_bg(entries)
    if hourly:
        print()
        print("── Average BG by hour ───────────────────────────────────────────────")
        for hour in sorted(hourly):
            avg = hourly[hour]
            flag = "  ← high" if avg > 180 else ("  ← low " if avg < 70 else "")
            print(f"  {hour:02d}:00  {avg:5.0f} mg/dL  {bar(avg, scale=10)}{flag}")

    print()
    print("Note: estimates only. Verify with your endocrinologist before changing settings.")


if __name__ == "__main__":
    main()
