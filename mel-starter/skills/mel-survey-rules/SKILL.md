---
name: mel-survey-rules
description: Decide whether a FarmerChat insight gets a follow-up survey and compute its send window, using the golden-set rules R1 to R8 for the MEL Survey Agent (AWS hackathon, Digital Green). Use this skill whenever the user is working on the MEL survey agent, insights.jsonl, golden_set.jsonl, survey or no survey decisions, scope checks, junk insights, send windows, season end, crop calendar, channel order, priority, weekly cap, or scheduler handoffs, even if they do not name the rules.
---

# MEL survey rules

This skill holds the deterministic half of the MEL Survey Agent: which insights get a survey, and when to send it. Everything here is plain code with no model call, so it is cheap and easy to test.

## When to use

- Deciding survey or no survey for one insight or the whole dataset.
- Computing the send window for a survey.
- Explaining why an insight got no survey.
- Building the scheduler handoff (channels, priority, cap, expiry).
- Reproducing or testing against the golden set.

## The decision, in order

Run `scripts/decide.py` (import `decide(insight)`). The first rule that matches wins.

- R1 junk_insight: concern is null, or query_summary, advice_summary or resolution_type is missing, or decision_episode_type is casual. Do not use quality_flag, it is practice-data labelling only.
- R3 nothing_to_verify: resolution_type is generic_information.
- R4 waiting_for_details: resolution_type is needs_more_context.
- R5 no_check_needed: verification.needed is false.
- R6 window_after_season: the send window opens after 31 March 2027.
- R7 crop_already_harvested: the window opens after the crop's last calendar end date (crop_calendar_bihar_rabi.csv, livestock has none).
- R8 survey.

R2 (critical_needs_human_call: severity critical goes to a human call, no survey) was removed on purpose. Severity is not a decision input: a critical insight follows R3 to R8 like any other. The rule codes keep their golden-set names, so there is no R2 in the list.

## Send window (golden set, decoded)

Dates count from the insight's calendar date. Start is 09:00 and end is 19:00 India time, clipped to 31 March 2027 19:00.

- two_to_five_days: day 2 to day 5. three_to_ten_days: day 3 to day 10. seven_to_fourteen_days: day 7 to day 14.
- next_day: day 1 to day 2 (from the brief, no golden example seen).
- seasonal: day 30 to day 45 (one golden example only, the brief says 21 to 45).
- same_day: start 4 hours after the insight, end 4 hours later. If the start would be 19:00 or later, use 09:00 the next day.

action_window_days is ignored for the window in the golden set. The brief says otherwise, see references/golden_vs_brief.md.

## Verification status

Run `python scripts/test_against_golden.py <golden_set.jsonl>` to re-check. On golden set version 1 it gives 144 of 144 decisions and 95 of 95 windows correct, not counting the 6 golden cases that expect R2. Those 6 are an intended difference since R2 was removed, and the script lists them separately rather than as errors. The golden set may be updated during the event, so re-run it after any update.

Known limits, say them out loud rather than hiding them:
- R7 is a hypothesis that fits the single golden row. On all 6,999 practice insights it fires once.
- same_day edge cases (start exactly at 19:00, start before 09:00) and the seasonal start come from very few rows.
- On all practice insights the result is R1 723, R3 1,288, R4 880, R5 1,315, R6 64, R7 1, R8 2,728. This is before the weekly cap.

## Not in the golden set (use the brief, rules_brief.json)

Channel order, wait hours, priority, weekly cap and expiry are not tested by the golden set. Take them from references/rules_brief.json:

- Channels: preferred_channel, then the verification method's channel, then whatsapp, then voice only for high priority. Wait 6, 24 or 48 hours for high, normal or low priority.
- Priority: high for escalate_to_expert, severity high or critical, or emergency intent. Low for plan_created and do_not_act.
- Cap: 1 survey per farmer per 7 days across channels, high priority exempt but still counted. Expiry 2 days after the window closes.

## Conflicts to raise with the user

Read references/golden_vs_brief.md before reporting results. The no_survey schema only allows two reason codes, but the golden set uses seven, so the schema cannot record junk. Output golden codes and flag the mismatch.
