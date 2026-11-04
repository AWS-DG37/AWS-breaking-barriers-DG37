---
name: mel-survey-rules
description: Decide whether a FarmerChat insight gets a follow-up survey for the MEL survey agents (AWS hackathon, Digital Green), using the golden-set rules R1, R3, R4, R5 and R8. Use this skill whenever the user is working on survey or no survey decisions, scope checks, junk insights, insights.jsonl or golden_set.jsonl, even if they do not name the rules. It does not cover timing: send windows, season end, crop calendar, channels, priority, caps and expiry belong to the separate timing script.
---

# MEL survey rules

This skill holds the deterministic gate of the MEL survey agents: which insights get a survey. It is plain code with no model call, so it is cheap and easy to test.

It does not decide when a survey is sent. A separate timing script owns everything about time: the send window, rules that need the window, channel order, priority, wait hours, the weekly cap and expiry. The agents output the survey questions only, and the timing script adds the send window.

## When to use

- Deciding survey or no survey for one insight or the whole dataset.
- Explaining why an insight got no survey.
- Reproducing or testing the decision against the golden set.

## The decision, in order

Run `scripts/decide.py` (import `decide(insight)`). The first rule that matches wins.

- R1 junk_insight: concern is null, or query_summary, advice_summary or resolution_type is missing, or decision_episode_type is casual. Do not use quality_flag, it is practice-data labelling only.
- R3 nothing_to_verify: resolution_type is generic_information.
- R4 waiting_for_details: resolution_type is needs_more_context.
- R5 no_check_needed: verification.needed is false.
- R8 survey.

Rule codes keep their golden-set names, so the gaps are deliberate:

- R2 critical_needs_human_call was removed on purpose. Severity is not a decision input, so a critical insight follows the other rules like any other.
- R6 window_after_season and R7 crop_already_harvested need the send window, so they belong to the timing script. An insight they would decline is a survey here, and the timing script may still drop it.

## Verification status

Run `python scripts/test_against_golden.py <golden_set.jsonl>` to re-check. On golden set version 1 it gives 141 of 141 decisions correct. The other 9 rows are listed separately, not as errors: 6 expect R2 (removed) and 3 expect R6 or R7 (timing script). The golden set may be updated during the event, so re-run it after any update.

On all 6,999 practice insights the result is R1 723, R3 1,288, R4 880, R5 1,315, R8 2,793. Compared with the old gate, 64 R6 and 1 R7 decisions are now surveys, pending the timing script.

## For the timing script

The source data it needs is in the participant pack, not in this skill: `docs/participant-pack/json/rules/rules.json` (send window days, window rules, priority, channel order, wait hours, send hours, frequency cap, expiry) and `docs/participant-pack/data/crop_calendar_bihar_rabi.csv` (for R7). references/golden_vs_brief.md lists where the golden set and the brief disagree on timing.

## Conflicts to raise with the user

Read references/golden_vs_brief.md before reporting results. The no_survey schema only allows two reason codes, but the golden set uses more, so the schema cannot record junk. Output golden codes and flag the mismatch.
