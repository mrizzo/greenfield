# greenfield

Downloads your Nightscout CGM data locally, empirically tunes ISF & basal rates from your own correction history, and — via the Greenfield agent — proposes adjustments with citations back to your own data.

Nightscout shows you your data. This tool tells you whether your **settings** are wrong — ISF, basal rates — derived from what actually happened in your own data, not population averages.

## Why "greenfield"

Named after **Dr. Michael Greenfield** — my endocrinologist of 14 years, who is himself Type 1 diabetic. The bar for the agent is his bar: *he wouldn't propose something he wouldn't do for himself.* So the `greenfield.py` agent only **proposes** (never applies), grounds every number in your own data, prefers small changes or none at all, and always leaves the decision to you.

(The repo grew out of a simple downloader — the `nightscout-dl.sh` script keeps that name; the repo is named for what it became.)

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

### `greenfield.py` — Claude-powered proposals
A read-only agent that reviews recent data (reusing the loaders and analyses above) and **proposes** basal/ISF adjustments with citations to the specific data behind each number. It never applies changes — you decide. Guardrails are enforced in code: it refuses with <7 days of data or >20% CGM gaps, rejects proposed numbers with no data citation or >15% changes that aren't justified, and logs every run (including refusals) to `proposals.jsonl` (gitignored). Not medical advice.

Needs the `anthropic` package and an `ANTHROPIC_API_KEY`. On Homebrew Python (PEP 668 blocks global `pip install`), install into a venv:

```sh
python3 -m venv .venv && .venv/bin/pip install anthropic
```

greenfield reads the key from the **environment** — it does not source `config` itself — so keep it in `config` as an `export`ed line and `source config` before running (see the alias below), or just `export` it in your shell.

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

# Propose basal/ISF changes for the last 14 days (needs ANTHROPIC_API_KEY + anthropic venv)
source config && .venv/bin/python greenfield.py 14

# Trim the highest- and lowest-TDD day from the Rule-of-1800 anchor
# (one heavy-meal or fasting day won't skew the ISF/carb sanity checks)
source config && .venv/bin/python greenfield.py 7 --trim-tdd

# Reprint the last run's output offline — no API key, no new API call
.venv/bin/python greenfield.py --last

# Run and also save the output to a file of your choice
source config && .venv/bin/python greenfield.py 14 --export report.txt
```

Output goes to `~/nightscout-exports/` by default (configurable in `config`).

### Handy alias

```sh
# ~/.zshrc — sources config (for ANTHROPIC_API_KEY) and uses the venv python
alias greenfield='cd /Users/mrizzo/code/nightscout-dl && source config && .venv/bin/python greenfield.py'
```
