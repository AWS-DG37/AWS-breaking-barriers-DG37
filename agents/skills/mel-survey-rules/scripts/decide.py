#!/usr/bin/env python3
"""MEL survey agents: decide survey or no survey for one insight. No timing.

The agents only decide WHETHER an insight gets a survey and write it. WHEN it is sent is
owned by a separate timing script, so this file computes no send window and applies no
rule that needs one. Rules removed from here and why:
  R2 critical_needs_human_call  removed on purpose (severity is not a decision input)
  R6 window_after_season        needs the send window -> timing script
  R7 crop_already_harvested     needs the send window -> timing script

Rule codes keep their golden-set names, so the remaining codes are R1, R3, R4, R5, R8.
Library use:  from decide import decide ; decide(insight_dict)
Script use:   python decide.py insights.jsonl   (prints decision counts)
"""
import json, sys

# Golden-set rules this file deliberately does not apply. Used by test_against_golden.py
# and sweep.py to score those rows as handled elsewhere instead of as errors.
REMOVED_RULES = {"R2"}
TIMING_RULES = {"R6", "R7"}


def decide(insight):
    """Return {survey: False, rule, reason_code} or {survey: True, rule: "R8"}."""
    i = insight
    if (i.get("concern") is None or not i.get("query_summary") or not i.get("advice_summary")
            or not i.get("resolution_type") or i.get("decision_episode_type") == "casual"):
        return {"survey": False, "rule": "R1", "reason_code": "junk_insight"}
    if i["resolution_type"] == "generic_information":
        return {"survey": False, "rule": "R3", "reason_code": "nothing_to_verify"}
    if i["resolution_type"] == "needs_more_context":
        return {"survey": False, "rule": "R4", "reason_code": "waiting_for_details"}
    if not i["verification"]["needed"]:
        return {"survey": False, "rule": "R5", "reason_code": "no_check_needed"}
    return {"survey": True, "rule": "R8"}


if __name__ == "__main__":
    from collections import Counter
    c = Counter()
    for line in open(sys.argv[1], encoding="utf-8"):
        if line.strip():
            c[decide(json.loads(line))["rule"]] += 1
    print(dict(sorted(c.items())))
