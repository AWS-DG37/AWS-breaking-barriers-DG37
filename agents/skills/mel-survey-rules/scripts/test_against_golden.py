#!/usr/bin/env python3
"""Compare decide() with the golden set. Usage: python test_against_golden.py golden_set.jsonl

This is a script, not a pytest module; the __main__ guard keeps pytest collection from running it.
The agents only decide survey or no survey, so send windows are not compared. Golden rows that
expect a removed rule (R2) or a timing rule (R6, R7, owned by the timing script) are listed
separately as intended differences, not counted as wrong.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decide import REMOVED_RULES, TIMING_RULES, decide  # noqa: E402


def main(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    elsewhere = REMOVED_RULES | TIMING_RULES
    scored = [r for r in rows if r["expected"]["rule"] not in elsewhere]
    bad = 0
    for r in scored:
        d, e = decide(r["insight"]), r["expected"]
        if d["rule"] != e["rule"] or d["survey"] != e["survey"]:
            bad += 1; print("RULE", r["golden_id"], "got", d["rule"], "expected", e["rule"])
    for r in rows:
        rule = r["expected"]["rule"]
        if rule in elsewhere:
            why = "removed rule" if rule in REMOVED_RULES else "timing script"
            print(f"NOT-HERE {r['golden_id']} golden expects {rule} ({why}), agents say {decide(r['insight'])['rule']}")
    print(f"decisions wrong: {bad}/{len(scored)}   "
          f"handled elsewhere: {len(rows) - len(scored)} "
          f"(removed {sum(r['expected']['rule'] in REMOVED_RULES for r in rows)}, "
          f"timing {sum(r['expected']['rule'] in TIMING_RULES for r in rows)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
