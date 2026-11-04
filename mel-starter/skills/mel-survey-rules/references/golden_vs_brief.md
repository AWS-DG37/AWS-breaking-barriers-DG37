# Where the golden set (v1) and the brief differ

| Topic | Brief (index.html, rules.json) | Golden set v1 | What decide.py does |
|---|---|---|---|
| Critical severity | priority high | no survey, critical_needs_human_call (R2) | follows the brief: R2 removed, critical insights follow the normal rules (6 golden rows differ on purpose) |
| verification.needed false | not mentioned | no survey, no_check_needed (R5) | follows golden |
| Window hours | 07:00 to 19:00 | 09:00 to 19:00 | follows golden |
| Window start | not before the action window ends, day 1 minimum | created date plus timing range, action_window_days ignored | follows golden |
| same_day | days 1 to 3 (brief) or 0 to 1 (rules.json) | 4 hours after the insight for 4 hours | follows golden |
| seasonal | 21 to 45 | 30 to 45 (one row) | follows golden, config in TIMING_DAYS |
| Junk | not a reason code | junk_insight (R1) | follows golden |
| no_survey reason_code | only generic_information, needs_more_context | seven other codes | emits golden codes |

Ask the organizers which source wins, since the golden set may be updated. The rules live in one file (scripts/decide.py), so a change is a one-line edit.
