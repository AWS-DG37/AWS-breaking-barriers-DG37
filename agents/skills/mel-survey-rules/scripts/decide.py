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

# Every rule this gate can return, with its no_survey reason code. R8 is the only survey rule.
# The classifier gate (agents/classify.py) answers with one of these keys too.
REASON_CODES = {"R1": "junk_insight", "R3": "nothing_to_verify", "R4": "waiting_for_details",
                "R5": "no_check_needed", "R8": None}


def decision(rule):
    """The decision dict for a rule code."""
    if rule == "R8":
        return {"survey": True, "rule": "R8"}
    return {"survey": False, "rule": rule, "reason_code": REASON_CODES[rule]}


def decide(insight):
    """Return {survey: False, rule, reason_code} or {survey: True, rule: "R8"}."""
    i = insight
    if (i.get("concern") is None or not i.get("query_summary") or not i.get("advice_summary")
            or not i.get("resolution_type") or i.get("decision_episode_type") == "casual"):
        return decision("R1")
    if i["resolution_type"] == "generic_information":
        return decision("R3")
    if i["resolution_type"] == "needs_more_context":
        return decision("R4")
    if not i["verification"]["needed"]:
        return decision("R5")
    return decision("R8")


if __name__ == "__main__":
    from collections import Counter
    c = Counter()
    for line in open(sys.argv[1], encoding="utf-8"):
        if line.strip():
            c[decide(json.loads(line))["rule"]] += 1
    print(dict(sorted(c.items())))
