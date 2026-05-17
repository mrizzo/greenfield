#!/usr/bin/env python3
"""Suggest ISF and basal rates from Nightscout CSV exports."""

import argparse
import bisect
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
                event_type = row.get("eventType", "")
                is_basal = "temp basal" in event_type.lower() or "basal" in event_type.lower()
                insulin = float(row["insulin"]) if row.get("insulin") else basal_delivered
                rows.append({
                    "dt": dt,
                    "type": event_type,
                    "insulin": insulin,
                    "bolus": 0.0 if is_basal else insulin,
                    "basal": insulin if is_basal else 0.0,
                    "carbs": float(row["carbs"]) if row.get("carbs") else 0.0,
                    "rate": rate,
                    "duration": duration,
                })
    return sorted(rows, key=lambda r: r["dt"])


def tdd_stats(treatments):
    by_day_total = defaultdict(float)
    by_day_bolus = defaultdict(float)
    by_day_basal = defaultdict(float)
    for t in treatments:
        if t["insulin"] > 0:
            d = t["dt"].date()
            by_day_total[d] += t["insulin"]
            by_day_bolus[d] += t["bolus"]
            by_day_basal[d] += t["basal"]
    if not by_day_total:
        return None, [], None, None
    days = sorted(by_day_total)
    totals = [by_day_total[d] for d in days]
    boluses = [by_day_bolus[d] for d in days]
    basals = [by_day_basal[d] for d in days]
    return statistics.median(totals), totals, statistics.median(boluses), statistics.median(basals)


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

    carb_times = sorted(t["dt"] for t in treatments if t["carbs"] > 0)
    window = timedelta(hours=2)

    by_block = defaultdict(list)
    for t in treatments:
        if t["insulin"] <= 0:
            continue
        lo = bisect.bisect_left(carb_times, t["dt"] - window)
        nearby_carbs = lo < len(carb_times) and carb_times[lo] <= t["dt"] + window
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
                block = (t["dt"].hour // 2) * 2
                by_block[block].append(observed)

    all_results = [v for vals in by_block.values() for v in vals]
    overall = (statistics.median(all_results), len(all_results)) if all_results else (None, 0)
    hourly = {block: (statistics.median(vals), len(vals)) for block, vals in by_block.items()}
    return overall, hourly


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


def hourly_insulin(treatments):
    bolus_by_hour = defaultdict(float)
    basal_by_hour = defaultdict(float)
    days = {t["dt"].date() for t in treatments if t["insulin"] > 0}
    n_days = len(days) or 1
    for t in treatments:
        if t["insulin"] > 0:
            h = t["dt"].hour
            bolus_by_hour[h] += t["bolus"]
            basal_by_hour[h] += t["basal"]
    hours = sorted(set(bolus_by_hour) | set(basal_by_hour))
    return {h: (bolus_by_hour[h] / n_days, basal_by_hour[h] / n_days) for h in hours}


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
    median_tdd, tdd_values, median_bolus, median_basal = tdd_stats(treatments)
    print("── Insulin ──────────────────────────────────────────────────────────")
    if median_tdd:
        isf_rule = 1700 / median_tdd
        tdd_min, tdd_max = min(tdd_values), max(tdd_values)
        print(f"  Median TDD:       {median_tdd:.1f} U/day  (range {tdd_min:.1f}–{tdd_max:.1f})")
        print(f"    Bolus:          {median_bolus:.1f} U/day")
        print(f"    Basal:          {median_basal:.1f} U/day")
        print(f"  ISF (1700 rule):  {isf_rule:.0f} mg/dL per unit")
    else:
        print("  No insulin data found.")

    (emp_isf, n_corrections), isf_by_block = empirical_isf(entries, treatments)
    if emp_isf:
        print(f"  ISF (observed):   {emp_isf:.0f} mg/dL per unit  ({n_corrections} corrections analysed)")

    # ── Basal estimate ────────────────────────────────────────────────────────
    print()
    print("── Basal estimate ───────────────────────────────────────────────────")
    if median_tdd:
        total_basal = median_tdd * 0.45
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

    # ── ISF by time block ─────────────────────────────────────────────────────
    if isf_by_block:
        print()
        print("── Observed ISF by time block (corrections only, ≥2 samples) ────────")
        for block in sorted(isf_by_block):
            isf, n = isf_by_block[block]
            if n < 2:
                continue
            marker = "  ← strong" if isf < 30 else ("  ← weak  " if isf > 80 else "")
            print(f"  {block:02d}:00–{block+2:02d}:00  {isf:3.0f} mg/dL/U  (n={n}){marker}")

    # ── Hourly average BG ─────────────────────────────────────────────────────
    hourly = hourly_avg_bg(entries)
    if hourly:
        print()
        print("── Average BG by hour ───────────────────────────────────────────────")
        for hour in sorted(hourly):
            avg = hourly[hour]
            flag = "  ← high" if avg > 180 else ("  ← low " if avg < 70 else "")
            print(f"  {hour:02d}:00  {avg:5.0f} mg/dL  {bar(avg, scale=10)}{flag}")

    # ── Hourly insulin delivery ───────────────────────────────────────────────
    ins_by_hour = hourly_insulin(treatments)
    if ins_by_hour:
        print()
        print("── Avg insulin delivery by hour (U/day averaged) ───────────────────")
        print(f"  {'hour':<6}  {'bolus':>5}  {'basal':>5}  {'total':>5}")
        max_u = max(b + s for b, s in ins_by_hour.values())
        scale = max_u / 20
        for hour in sorted(ins_by_hour):
            bolus, basal = ins_by_hour[hour]
            total = bolus + basal
            bolus_bar = "▓" * min(int(bolus / scale), 20)
            basal_bar = "░" * min(int(basal / scale), 20)
            print(f"  {hour:02d}:00   {bolus:4.2f}   {basal:4.2f}   {total:4.2f}  {bolus_bar}{basal_bar}")

    print()
    print("Note: estimates only. Verify with your endocrinologist before changing settings.")


if __name__ == "__main__":
    main()
