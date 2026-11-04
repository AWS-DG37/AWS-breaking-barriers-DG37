#!/usr/bin/env python3
"""Compare decide() with the golden set. Usage: python test_against_golden.py golden_set.jsonl

This is a script, not a pytest module; the __main__ guard keeps pytest collection from running it.
Rule R2 (critical severity -> human call) was removed on purpose, so golden rows that expect R2
are listed separately as intended differences, not counted as wrong.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decide import decide  # noqa: E402

REMOVED_RULES = {"R2"}


def main(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    scored = [r for r in rows if r["expected"]["rule"] not in REMOVED_RULES]
    removed = [r for r in rows if r["expected"]["rule"] in REMOVED_RULES]
    bad_rule = bad_win = 0
    for r in scored:
        d, e = decide(r["insight"]), r["expected"]
        if d["rule"] != e["rule"] or d["survey"] != e["survey"]:
            bad_rule += 1; print("RULE", r["golden_id"], "got", d["rule"], "expected", e["rule"])
        elif e["survey"]:
            if d["send_window"]["start"] != e["send_window"]["start"] or d["send_window"]["end"] != e["send_window"]["end"]:
                bad_win += 1; print("WINDOW", r["golden_id"], d["send_window"], e["send_window"])
    for r in removed:
        print("REMOVED-RULE", r["golden_id"], "golden expects", r["expected"]["rule"], "now", decide(r["insight"])["rule"])
    print(f"decisions wrong: {bad_rule}/{len(scored)}   "
          f"windows wrong: {bad_win}/{sum(r['expected']['survey'] for r in scored)}   "
          f"intended differences (removed rules): {len(removed)}")
    return 1 if bad_rule or bad_win else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
