# nightscout-dl

Downloads your Nightscout CGM data locally and empirically tunes ISF & basal rates from your own correction history.

Nightscout shows you your data. This tool tells you whether your **settings** are wrong — ISF, basal rates — derived from what actually happened in your own data, not population averages.

## What it does

### `nightscout-dl.sh` — daily data export
Pulls the previous day's data from your Nightscout instance and saves it locally as CSV/JSON:

| File | Contents |
|------|----------|
| `YYYY-MM-DD_entries.csv` | CGM readings — glucose (mg/dL), trend direction, noise, device |
| `YYYY-MM-DD_treatments.csv` | Insulin doses, carb entries, temp basals, notes |
| `YYYY-MM-DD_profile.json` | Basal schedule, ISF, carb ratios |

### `analyze.py` — settings tuning
Reads the exported files and produces three analyses Nightscout has no equivalent for:

**Empirical ISF by time block** — finds every correction bolus with no carbs within ±2 hours, measures how far BG actually dropped in the next 3 hours, and reports your real observed ISF for each 2-hour window of the day.

**Fasting basal drift** — identifies stretches with no insulin or carbs for 4+ hours and measures BG rate of change. Rising BG = basal too low. Falling BG = basal too high.

**Bolus vs. basal breakdown** — reconstructs scheduled basal delivery from your profile, accounts for every temp basal override, and gives you accurate bolus/basal split and total daily dose.

## Setup

```bash
cp config.example config
# edit config: set NIGHTSCOUT_URL and API_SECRET (or NS_TOKEN)
bash install.sh   # sets up a daily launchd job
```

Requires `jq` and `curl` (`brew install jq`).

## Usage

```bash
# Download yesterday's data
./nightscout-dl.sh

# Download a specific date
./nightscout-dl.sh -d 2026-05-20

# Analyze the last 14 days
python3 analyze.py

# Analyze a specific number of days
python3 analyze.py -n 30

# Analyze a single day
python3 analyze.py -d 2026-05-20
```

Output goes to `~/nightscout-exports/` by default (configurable in `config`).
