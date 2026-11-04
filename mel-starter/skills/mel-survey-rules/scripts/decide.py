#!/usr/bin/env python3
"""MEL survey agent: decide survey / no survey and compute the send window.

Rules follow the golden set (version 1), decoded from data/golden_set.jsonl.
Library use:  from decide import decide ; decide(insight_dict)
Script use:   python decide.py insights.jsonl   (prints decision counts)
"""
import csv, json, os, sys
from datetime import datetime, timedelta, time

IST = "+05:30"
HERE = os.path.dirname(os.path.abspath(__file__))
CAL = os.path.join(HERE, "..", "references", "crop_calendar_bihar_rabi.csv")
SEASON_END = datetime.fromisoformat("2027-03-31T19:00:00" + IST)

# (start_day, end_day) counted from the insight's calendar date.
# same_day is handled separately. The seasonal start of 30 comes from ONE golden row.
TIMING_DAYS = {
    "next_day": (1, 2),
    "two_to_five_days": (2, 5),
    "three_to_ten_days": (3, 10),
    "seven_to_fourteen_days": (7, 14),
    "seasonal": (30, 45),
}

def _calendar():
    last = {}
    with open(CAL, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = datetime.fromisoformat(r["end_date"] + "T19:00:00" + IST)
            if r["crop"] not in last or d > last[r["crop"]]:
                last[r["crop"]] = d
    return last
CROP_LAST_DAY = _calendar()

def _at(day, hh, mm=0):
    return datetime.combine(day.date(), time(hh, mm), tzinfo=day.tzinfo)

def send_window(insight):
    """Return (start, end) datetimes, or None if timing is unknown."""
    created = datetime.fromisoformat(insight["created_at"])
    timing = insight["verification"]["timing"]
    if timing == "same_day":
        start = created + timedelta(hours=4)
        if start.time() >= time(19, 0):
            start = _at(created + timedelta(days=1), 9)
        elif start.time() < time(9, 0):
            start = _at(start, 9)
        return start, start + timedelta(hours=4)
    if timing in TIMING_DAYS:
        a, b = TIMING_DAYS[timing]
        return _at(created + timedelta(days=a), 9), _at(created + timedelta(days=b), 19)
    return None

def decide(insight):
    """Return {survey, rule, reason_code} or {survey, rule, send_window}."""
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
    w = send_window(i)
    if w is None:
        return {"survey": False, "rule": "R5", "reason_code": "no_check_needed"}
    start, end = w
    if start > SEASON_END:
        return {"survey": False, "rule": "R6", "reason_code": "window_after_season"}
    last = CROP_LAST_DAY.get(i["asset"]["name"])
    if last is not None and start > last:
        return {"survey": False, "rule": "R7", "reason_code": "crop_already_harvested"}
    end = min(end, SEASON_END)
    return {"survey": True, "rule": "R8",
            "send_window": {"start": start.isoformat(), "end": end.isoformat()}}

if __name__ == "__main__":
    from collections import Counter
    c = Counter()
    for line in open(sys.argv[1], encoding="utf-8"):
        c[decide(json.loads(line))["rule"]] += 1
    print(dict(sorted(c.items())))
