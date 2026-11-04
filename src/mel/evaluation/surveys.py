"""Score a worker results file against the golden set: decisions, survey shape, speed.

Deterministic only. Question meaning still needs an LLM judge or human review; the
side-by-side rows in the report are for that reading.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import mean

from mel.evaluation.golden import GoldenCase

GENERATED = ("accepted", "rejected")  # The worker intended a survey for these.


def load_results(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _mean(values: list[float]) -> float | None:
    return _round(mean(values)) if values else None


def score_surveys(results: list[dict], cases: list[GoldenCase]) -> dict:
    by_insight = {case.insight["insight_id"]: case for case in cases}
    unknown = [r["insight_id"] for r in results if r["insight_id"] not in by_insight]
    if unknown:
        raise ValueError(f"results contain insights missing from the golden file: {unknown}")
    status = Counter(r["status"] for r in results)
    decision_hits = 0
    item_counts, golden_item_counts, option_counts, relevance = [], [], [], []
    follow_ups: Counter = Counter()
    rows = []
    for result in results:
        case = by_insight[result["insight_id"]]
        intended = result["status"] in GENERATED
        decision_hits += intended == case.expects_survey
        survey = result.get("survey") or result.get("candidate_survey")
        if survey and case.expects_survey:
            item_counts.append(len(survey["items"]))
            golden_item_counts.append(len(case.expected["items"]))
        for item in survey["items"] if survey else []:
            options = item.get("options", [])
            option_counts.append(len(options))
            follow_ups.update(o["follow_up"] for o in options if o.get("follow_up"))
        if result.get("review"):
            relevance.append(result["review"]["relevance_score"])
        rows.append(_row(result, case, survey))
    usage_complete = all(r.get("usage_complete", False) for r in results)
    elapsed = [r["elapsed_seconds"] for r in results if r["status"] in GENERATED]
    return {
        "cases": len(results),
        "status": dict(status),
        "decision_accuracy": _round(decision_hits / len(results)) if results else None,
        "decision_correct": decision_hits,
        "items": {
            "mean": _mean(item_counts),
            "golden_mean": _mean(golden_item_counts),
        },
        "options": {
            "mean_per_question": _mean(option_counts),
            "max": max(option_counts, default=None),
            "share_at_most_4": _round(
                sum(n <= 4 for n in option_counts) / len(option_counts) if option_counts else None
            ),
            "at_most_4": sum(n <= 4 for n in option_counts),
            "questions": len(option_counts),
        },
        "follow_up_codes": dict(follow_ups),
        "review_relevance_mean": _mean(relevance),
        "reviewed": len(relevance),
        "seconds_per_survey": {"mean": _mean(elapsed), "max": max(elapsed, default=None)},
        "tokens": {
            "complete": usage_complete,
            "input": sum(r["input_tokens"] for r in results) if usage_complete else None,
            "output": sum(r["output_tokens"] for r in results) if usage_complete else None,
        },
        "side_by_side": rows,
    }


def _row(result: dict, case: GoldenCase, survey: dict | None) -> dict:
    row = {
        "golden_id": case.golden_id,
        "insight_id": case.insight["insight_id"],
        "resolution_type": case.insight["resolution_type"],
        "advice": case.insight["advice_summary"],
        "status": result["status"],
        "rule": result.get("rule"),
        "golden_rule": case.expected["rule"],
    }
    if case.expects_survey:
        row["golden_items"] = [
            {"text": item["text"], "options": item["options"]} for item in case.expected["items"]
        ]
    if survey:
        row["items"] = [
            {
                "prompt": item["prompt"],
                "options": [
                    f"{o['label']} [{o['follow_up']}]" if o.get("follow_up") else o["label"]
                    for o in item.get("options", [])
                ],
            }
            for item in survey["items"]
        ]
    if result.get("review"):
        row["review"] = {
            "relevance": result["review"]["relevance_score"],
            "issues": result["review"]["issues"],
            "suggestions": result["review"].get("suggestions", []),
        }
    return row
