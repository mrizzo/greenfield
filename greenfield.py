#!/usr/bin/env python3
"""Greenfield — a Claude-powered agent that reviews recent Nightscout data and
*proposes* basal/ISF adjustments with citations. It never applies changes.

Usage:
    python3 greenfield.py [days]      # default 14, matching analyze.py

Requires ANTHROPIC_API_KEY in the environment (see config.example). The hard
guardrails (data-sufficiency refusal, proposal validation, mandatory logging)
are enforced in this file and greenfield_tools.py — not left to the prompt.
"""

import argparse
import json
import os
import sys

import anthropic

import greenfield_tools as gt

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000
MAX_ITERATIONS = 16  # safety cap on the tool-use loop
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Tool schemas exposed to Claude ───────────────────────────────────────────
TOOLS = [
    {
        "name": "fetch_recent_data",
        "description": "Download the last N days of Nightscout data (CGM, "
                       "treatments, profile) via the existing downloader. Use "
                       "this if the local export is stale or missing days.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer",
                                    "description": "Number of days to download"}},
            "required": ["days"],
        },
    },
    {
        "name": "get_tdd_series",
        "description": "Per-day total daily dose (TDD) over the window plus the "
                       "trailing median TDD. Use to judge TDD stability before "
                       "proposing changes.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "get_basal_segments",
        "description": "Hourly bolus/basal insulin delivery for a single day.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string",
                                    "description": "Day to inspect, YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "find_cgm_gaps",
        "description": "CGM gaps longer than 30 minutes in the window, "
                       "cross-referenced against pump-suspend events when "
                       "available. Use to judge data quality.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "get_fasting_drift",
        "description": "BG drift by 2-hour block during fasting periods. Positive "
                       "= BG rising = basal likely too low; negative = too high. "
                       "The primary signal for basal adjustments.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "get_observed_isf",
        "description": "Empirically observed ISF by 2-hour block, from real "
                       "correction boluses (no carbs within ±2h). The primary "
                       "signal for ISF adjustments.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "get_hourly_bg",
        "description": "Average CGM by hour of day over the window. Works on "
                       "closed-loop data where fasting-drift/observed-ISF are "
                       "empty; a block that runs persistently high or low is a "
                       "candidate for a basal/ISF change there.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "get_current_settings",
        "description": "Current basal / ISF / carb-ratio profile, read from the "
                       "latest downloaded Nightscout profile. Read-only.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_proposal",
        "description": "Record your final proposal. MUST be called exactly once "
                       "per run, including when you propose nothing "
                       "(no_change: true). The system validates it in code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string",
                            "description": "What you found + overall recommendation"},
                "no_change": {"type": "boolean",
                              "description": "true if you propose no changes"},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "setting": {"type": "string",
                                        "enum": ["basal", "isf", "carb_ratio"]},
                            "time_block": {"type": "string",
                                           "description": "'HH:MM-HH:MM' or 'all-day'"},
                            "current_value": {"type": "number"},
                            "proposed_value": {"type": "number"},
                            "citations": {"type": "array", "items": {"type": "string"},
                                          "description": "specific dates/values that "
                                                         "justify this number"},
                            "large_change": {"type": "boolean"},
                            "justification": {"type": "string"},
                        },
                        "required": ["setting", "time_block", "current_value",
                                     "proposed_value", "citations"],
                    },
                },
            },
            "required": ["summary"],
        },
    },
]


def _text(response):
    return "".join(b.text for b in response.content if b.type == "text").strip()


def main():
    parser = argparse.ArgumentParser(
        description="Propose basal/ISF adjustments from recent Nightscout data")
    parser.add_argument("days", nargs="?", type=int, default=14,
                        help="Window size in days (default: 14)")
    args = parser.parse_args()
    days = args.days

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ERROR: set ANTHROPIC_API_KEY in your environment "
                 "(see config.example).")

    # ── HARD GUARDRAIL: data sufficiency. Enforced here, before any API call. ─
    suff = gt.data_sufficiency(days)
    if not suff["ok"]:
        reason = "insufficient_data: " + "; ".join(suff["reasons"])
        gt.log_proposal(
            {"no_change": True,
             "summary": f"Refused to propose: {reason}."},
            refusal_reason=reason,
            meta={"window_days": days, "data_sufficiency": suff},
        )
        print(f"Greenfield declined to propose.\n  {reason}")
        print(f"  ({suff['days_available']} days available, "
              f"{suff['gap_fraction']:.0%} CGM gap coverage)")
        print(f"Logged to {gt.PROPOSALS_LOG}")
        return

    # Values the code-side validator needs.
    current_settings = gt.get_current_settings()
    tdd_series = gt.get_tdd_series(days)
    trailing_tdd = tdd_series.get("trailing_median_tdd")

    system_prompt = open(os.path.join(SCRIPT_DIR, "greenfield_prompt.md")).read()
    client = anthropic.Anthropic()

    messages = [{
        "role": "user",
        "content": (
            f"Review my last {days} days of Nightscout data and propose basal / "
            f"ISF adjustments, with citations to the specific data behind each "
            f"number. Use the tools to gather evidence. Data-sufficiency has "
            f"already passed ({suff['days_available']} days, "
            f"{suff['gap_fraction']:.0%} gaps). Finish by calling log_proposal "
            f"exactly once."
        ),
    }]

    # Handlers that take the tool input dict and return a JSON-serializable result.
    dispatch = {
        "fetch_recent_data": lambda i: gt.fetch_recent_data(i["days"]),
        "get_tdd_series": lambda i: gt.get_tdd_series(i["days"]),
        "get_basal_segments": lambda i: gt.get_basal_segments(i["date"]),
        "find_cgm_gaps": lambda i: gt.find_cgm_gaps(i["days"]),
        "get_fasting_drift": lambda i: gt.get_fasting_drift(i["days"]),
        "get_observed_isf": lambda i: gt.get_observed_isf(i["days"]),
        "get_hourly_bg": lambda i: gt.get_hourly_bg(i["days"]),
        "get_current_settings": lambda i: gt.get_current_settings(),
    }

    logged_ok = False
    last_attempt = None  # (proposal, validation) of the most recent submission

    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            thinking={"type": "adaptive"},
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            print("Claude refused this request.")
            break
        if response.stop_reason != "tool_use":
            break  # end_turn (or max_tokens)

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "log_proposal":
                proposal = dict(block.input)
                validation = gt.validate_proposal(proposal, tdd=trailing_tdd)
                last_attempt = (proposal, validation)
                if validation["ok"]:
                    gt.log_proposal(
                        proposal, validation=validation,
                        meta={"window_days": days, "model": MODEL,
                              "trailing_median_tdd": trailing_tdd},
                    )
                    logged_ok = True
                    result = {"logged": True, "flags": validation["flags"]}
                else:
                    # Reject in code — Claude must revise and resubmit.
                    result = {"logged": False, "rejected": True,
                              "rejections": validation["rejections"],
                              "flags": validation["flags"]}
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result),
                    "is_error": not validation["ok"],
                })
            else:
                try:
                    out = dispatch[block.name](dict(block.input))
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": json.dumps(out, default=str),
                    })
                except Exception as e:  # surface tool errors back to Claude
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": f"tool error: {e}", "is_error": True,
                    })

        messages.append({"role": "user", "content": tool_results})

    # ── HARD GUARDRAIL: log_proposal must fire on every run. ─────────────────
    if not logged_ok:
        if last_attempt is not None:
            proposal, validation = last_attempt
            gt.log_proposal(
                proposal, validation=validation,
                refusal_reason="no valid proposal submitted",
                meta={"window_days": days, "model": MODEL,
                      "note": "logging the model's last (invalid) attempt"},
            )
        else:
            gt.log_proposal(
                {"no_change": True,
                 "summary": "Run ended without the model submitting a proposal."},
                refusal_reason="no proposal submitted",
                meta={"window_days": days, "model": MODEL},
            )

    print("\n" + "=" * 70)
    print(_text(response) or "(no final text)")
    print("=" * 70)
    print(f"Proposal logged to {gt.PROPOSALS_LOG}")


if __name__ == "__main__":
    main()
