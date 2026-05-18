#!/usr/bin/env python3
"""Suggest ISF and basal rates from Nightscout CSV exports."""

import argparse
import bisect
import csv
import glob
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_dt(date_str, time_str):
    try:
        return datetime.fromisoformat(f"{date_str}T{time_str}")
    except ValueError:
        return None


def epoch_ms_to_dt(epoch_ms, tz=None):
    dt_utc = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return dt_utc.astimezone(tz).replace(tzinfo=None) if tz else dt_utc.replace(tzinfo=None)


def load_entries(directory, tz=None):
    rows = []
    for f in sorted(glob.glob(os.path.join(directory, "*_entries.csv"))):
        with open(f, newline="") as fh:
            for row in csv.DictReader(fh):
                if not row.get("sgv_mgdl"):
                    continue
                try:
                    dt = epoch_ms_to_dt(int(float(row["epoch_ms"])), tz)
                    rows.append({"dt": dt, "sgv": int(row["sgv_mgdl"])})
                except (ValueError, KeyError):
                    pass
    return sorted(rows, key=lambda r: r["dt"])


def load_treatments(directory, tz=None):
    rows = []
    for f in sorted(glob.glob(os.path.join(directory, "*_treatments.csv"))):
        with open(f, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    dt = epoch_ms_to_dt(int(float(row["epoch_ms"])), tz)
                except (KeyError, ValueError):
                    dt = parse_dt(row.get("date", ""), row.get("time", ""))
                if not dt:
                    continue
                rate = float(row["rate"]) if row.get("rate") else 0.0
                duration = float(row["duration"]) if row.get("duration") else 0.0
                event_type = row.get("eventType", "")
                is_basal = event_type.lower() == "temp basal"
                rate_delivered = rate * duration / 60 if rate and duration else 0.0
                insulin = float(row["insulin"]) if row.get("insulin") else (0.0 if is_basal else rate_delivered)
                rows.append({
                    "dt": dt,
                    "type": event_type,
                    "insulin": insulin,
                    "bolus": 0.0 if is_basal else insulin,
                    "basal": 0.0,
                    "carbs": float(row["carbs"]) if row.get("carbs") else 0.0,
                    "rate": rate,
                    "duration": duration,
                    "is_basal": is_basal,
                })

    rows = sorted(rows, key=lambda r: r["dt"])

    # Compute actual basal delivery: active time = time until next temp basal, capped at programmed duration
    temp_basals = [r for r in rows if r["is_basal"]]
    for i, t in enumerate(temp_basals):
        if t["rate"] == 0.0:
            continue
        programmed_min = t["duration"]
        if i + 1 < len(temp_basals):
            gap_min = (temp_basals[i + 1]["dt"] - t["dt"]).total_seconds() / 60
            active_min = min(programmed_min, gap_min)
        else:
            active_min = programmed_min
        delivered = t["rate"] * active_min / 60
        t["basal"] = delivered
        t["insulin"] = delivered

    return rows


def tdd_stats(treatments):
    by_day_bolus = defaultdict(float)
    by_day_base = defaultdict(float)
    by_day_pos = defaultdict(float)
    by_day_neg = defaultdict(float)
    for t in treatments:
        d = t["dt"].date()
        if t.get("type") == "Scheduled Basal":
            by_day_base[d] += t["basal"]
        elif t.get("is_basal"):
            if t["basal"] >= 0:
                by_day_pos[d] += t["basal"]
            else:
                by_day_neg[d] += t["basal"]
        elif t["insulin"] > 0:
            by_day_bolus[d] += t["bolus"]
    all_days = sorted(set(by_day_base) | set(by_day_bolus) | set(by_day_pos) | set(by_day_neg))
    if not all_days:
        return None
    def med(d): return statistics.median([d[day] for day in all_days]) if d else 0.0
    base = med(by_day_base)
    pos  = med(by_day_pos)
    neg  = med(by_day_neg)
    bolus = med(by_day_bolus)
    total_basal = base + pos + neg
    totals = [by_day_base[d] + by_day_pos[d] + by_day_neg[d] + by_day_bolus[d] for d in all_days]
    return {
        "median_tdd": statistics.median(totals),
        "tdd_range": (min(totals), max(totals)),
        "bolus": bolus,
        "base": base,
        "pos_temp": pos,
        "neg_temp": neg,
        "total_basal": total_basal,
    }


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
    active_times = sorted(t["dt"] for t in treatments if t["insulin"] > 0 or t["carbs"] > 0)

    for i in range(len(entries) - 1):
        a, b = entries[i], entries[i + 1]
        gap_min = (b["dt"] - a["dt"]).total_seconds() / 60
        if gap_min < 1 or gap_min > 10:
            continue

        window_start = a["dt"] - timedelta(hours=4)
        lo = bisect.bisect_left(active_times, window_start)
        if lo < len(active_times) and active_times[lo] <= b["dt"]:
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
    days = {t["dt"].date() for t in treatments if t.get("type") == "Scheduled Basal"}
    n_days = len(days) or 1
    for t in treatments:
        h = t["dt"].hour
        if t["bolus"] > 0:
            bolus_by_hour[h] += t["bolus"]
        if t["basal"] != 0:
            basal_by_hour[h] += t["basal"]
    hours = sorted(set(bolus_by_hour) | set(basal_by_hour))
    return {h: (bolus_by_hour[h] / n_days, basal_by_hour[h] / n_days) for h in hours}


def load_profiles(directory):
    """Return (profiles_dict, timezone) where profiles_dict maps date -> basal schedule."""
    profile_files = sorted(glob.glob(os.path.join(directory, "*_profile.json")))
    result = {}
    tz = None
    for path in profile_files:
        fname = os.path.basename(path)
        try:
            date = datetime.strptime(fname[:10], "%Y-%m-%d").date()
            with open(path) as f:
                data = json.load(f)
            store = data[0]['store']['default']
            basal = store['basal']
            result[date] = sorted((e['timeAsSeconds'], e['value']) for e in basal)
            if tz is None and store.get('timezone'):
                try:
                    tz = ZoneInfo(store['timezone'])
                except ZoneInfoNotFoundError:
                    pass
        except (KeyError, IndexError, json.JSONDecodeError, ValueError):
            continue
    return result, tz


def profile_for_date(profiles, date):
    """Return the basal schedule for the most recent profile on or before date."""
    candidates = [d for d in profiles if d <= date]
    if not candidates:
        return profiles[min(profiles)] if profiles else None
    return profiles[max(candidates)]


def _scheduled_rate_at(schedule, seconds_from_midnight):
    rate = schedule[0][1]
    for secs, value in schedule:
        if secs <= seconds_from_midnight:
            rate = value
        else:
            break
    return rate


def add_scheduled_basal(treatments, profiles, dates):
    """
    Mirror Nightscout's method: add full scheduled basal for every hour of each
    day, then let each temp basal entry contribute only its offset
    (actual_rate - scheduled_rate) * active_time.  Sum = correct total basal.
    """
    extra = []
    tb_by_date = defaultdict(list)
    for t in treatments:
        if t["is_basal"]:
            tb_by_date[t["dt"].date()].append(t)

    for date in dates:
        basal_schedule = profile_for_date(profiles, date)
        if not basal_schedule:
            continue
        day_start = datetime(date.year, date.month, date.day)
        day_end = day_start + timedelta(days=1)

        # 1. Add full scheduled basal broken into hour-aligned segments
        current = day_start
        while current < day_end:
            secs = (current - day_start).total_seconds()
            rate = _scheduled_rate_at(basal_schedule, secs)
            next_hour = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            next_schedule = day_end
            for break_secs, _ in basal_schedule:
                bp = day_start + timedelta(seconds=break_secs)
                if bp > current:
                    next_schedule = min(next_schedule, bp)
                    break
            seg_end = min(next_hour, next_schedule, day_end)
            duration_min = (seg_end - current).total_seconds() / 60
            delivered = rate * duration_min / 60
            extra.append({
                "dt": current,
                "type": "Scheduled Basal",
                "insulin": delivered,
                "bolus": 0.0,
                "basal": delivered,
                "carbs": 0.0,
                "rate": rate,
                "duration": duration_min,
                "is_basal": True,
            })
            current = seg_end

        day_tbs = sorted(tb_by_date[date], key=lambda t: t["dt"])
        for i, tb in enumerate(day_tbs):
            start = max(tb["dt"], day_start)
            if start >= day_end:
                continue
            programmed_end = tb["dt"] + timedelta(minutes=tb["duration"])
            next_start = day_tbs[i + 1]["dt"] if i + 1 < len(day_tbs) else day_end
            end = min(programmed_end, next_start, day_end)
            if end <= start:
                continue
            duration_hr = (end - start).total_seconds() / 3600
            sched_rate = _scheduled_rate_at(basal_schedule, (start - day_start).total_seconds())
            offset = (tb["rate"] - sched_rate) * duration_hr
            tb["basal"] = offset
            tb["insulin"] = offset

    return sorted(treatments + extra, key=lambda r: r["dt"])


def bar(value, scale=10, width=20):
    filled = min(int(abs(value) / scale), width)
    return "█" * filled


def main():
    parser = argparse.ArgumentParser(description="Suggest ISF and basal rates from Nightscout exports")
    parser.add_argument("directory", nargs="?",
                        default=os.path.expanduser("~/nightscout-exports"),
                        help="Directory containing exported CSVs (default: ~/nightscout-exports)")
    parser.add_argument("-n", "--days", type=int, default=14,
                        help="Number of most-recent days to analyse (default: 14, 0 = all)")
    parser.add_argument("-d", "--date", type=str, default=None,
                        help="Single day to analyse (YYYY-MM-DD); overrides -n")
    args = parser.parse_args()

    if args.date:
        try:
            single_day = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: date must be YYYY-MM-DD, got: {args.date}")
            return

    print(f"Analyzing: {args.directory}\n")

    profiles, tz = load_profiles(args.directory)
    entries = load_entries(args.directory, tz=tz)
    treatments = load_treatments(args.directory, tz=tz)

    if args.date:
        entries = [e for e in entries if e["dt"].date() == single_day]
        treatments = [t for t in treatments if t["dt"].date() == single_day]
    elif args.days > 0 and entries:
        all_days = sorted({e["dt"].date() for e in entries})
        cutoff = all_days[-min(args.days, len(all_days))]
        entries = [e for e in entries if e["dt"].date() >= cutoff]
        treatments = [t for t in treatments if t["dt"].date() >= cutoff]

    if not entries:
        print("No entries found. Run nightscout-dl.sh first.")
        return

    if profiles:
        dates = sorted({e["dt"].date() for e in entries})
        treatments = add_scheduled_basal(treatments, profiles, dates)
    else:
        print("Warning: no profile found — scheduled basal gaps will not be filled.")

    days = len({e["dt"].date() for e in entries})
    date_range = f"{entries[0]['dt'].date()} → {entries[-1]['dt'].date()}"
    print(f"  {days} days  |  {date_range}  |  {len(entries)} readings  |  {len(treatments)} treatments\n")

    # ── TDD & ISF ─────────────────────────────────────────────────────────────
    stats = tdd_stats(treatments)
    print("── Insulin ──────────────────────────────────────────────────────────")
    if stats:
        tdd_min, tdd_max = stats["tdd_range"]
        print(f"  Median TDD:       {stats['median_tdd']:.1f} U/day  (range {tdd_min:.1f}–{tdd_max:.1f})")
        print(f"    Bolus:          {stats['bolus']:.1f} U/day")
        print(f"    Base basal:     {stats['base']:.1f} U/day")
        print(f"    Pos temp:      +{stats['pos_temp']:.1f} U/day  (treatments only)")
        print(f"    Neg temp:       {stats['neg_temp']:.1f} U/day  (treatments only)")
        print(f"    Total basal:    {stats['total_basal']:.1f} U/day")
        print(f"  ISF (1700 rule):  {1700 / stats['median_tdd']:.0f} mg/dL per unit")
    else:
        print("  No insulin data found.")

    (emp_isf, n_corrections), isf_by_block = empirical_isf(entries, treatments)
    if emp_isf:
        print(f"  ISF (observed):   {emp_isf:.0f} mg/dL per unit  ({n_corrections} corrections analysed)")

    # ── Basal estimate ────────────────────────────────────────────────────────
    print()
    print("── Basal estimate ───────────────────────────────────────────────────")
    if stats:
        total_basal = stats["total_basal"]
        print(f"  Total basal/day:  ~{total_basal:.1f} U")
        print(f"  Flat rate:        ~{total_basal / 24:.2f} U/hr")

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
            print(f"  {block:02d}:00–{block+2:02d}:00  {rate:+.1f} mg/dL/hr  {verdict}  {bar(rate, scale=5)}")

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
        max_u = max(b + max(s, 0) for b, s in ins_by_hour.values())
        scale = max_u / 20 if max_u else 1
        for hour in sorted(ins_by_hour):
            bolus, basal = ins_by_hour[hour]
            total = bolus + basal
            bolus_bar = "▓" * min(int(bolus / scale), 20)
            basal_bar = "░" * min(int(max(basal, 0) / scale), 20)
            low_flag = "  ← low temp" if basal < 0 else ""
            print(f"  {hour:02d}:00   {bolus:4.2f}   {basal:5.2f}   {total:4.2f}  {bolus_bar}{basal_bar}{low_flag}")

    print()
    print("Note: I'm just a Python script, but honestly I looked at your actual data — which already puts me ahead of most endocrinologists. Use common sense.")


if __name__ == "__main__":
    main()
