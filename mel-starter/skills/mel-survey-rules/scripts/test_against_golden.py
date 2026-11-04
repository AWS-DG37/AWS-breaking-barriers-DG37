#!/usr/bin/env python3
"""Compare decide() with the golden set. Usage: python test_against_golden.py golden_set.jsonl"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decide import decide
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
bad_rule = bad_win = 0
for r in rows:
    d, e = decide(r["insight"]), r["expected"]
    if d["rule"] != e["rule"] or d["survey"] != e["survey"]:
        bad_rule += 1; print("RULE", r["golden_id"], "got", d["rule"], "expected", e["rule"])
    elif e["survey"]:
        if d["send_window"]["start"] != e["send_window"]["start"] or d["send_window"]["end"] != e["send_window"]["end"]:
            bad_win += 1; print("WINDOW", r["golden_id"], d["send_window"], e["send_window"])
print(f"decisions wrong: {bad_rule}/{len(rows)}   windows wrong: {bad_win}/{sum(r['expected']['survey'] for r in rows)}")
