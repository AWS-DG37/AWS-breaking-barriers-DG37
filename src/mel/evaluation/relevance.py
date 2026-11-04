"""LLM-judge relevance score for generated surveys, calibrated on the dev half only.

The judge reads saved worker results; it never runs the worker. Model calls happen
only in `mel-worker eval calibrate-relevance` and `mel-worker eval relevance`.

Calibration builds known-answer controls from dev golden rows: the row's own
reference questions (a good survey), a reference from an unrelated dev row, a
generic app question, and the own reference with injected new advice, an injected
personal-information question, or follow-ups on positive answers. The judge must
agree with each control's expected verdicts. Scoring the test half requires a
passing calibration for the same rubric, judge model and split.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mel.evaluation.policy import Assessed, Measurement, NotAssessed, load_split
from mel.evaluation.split import normalize_advice

RUBRIC_PATH = Path("eval/relevance_rubric.txt")
CRITERIA = (
    "advice_grounding", "measures_adoption", "measures_outcome",
    "follow_up_routing", "no_new_advice", "no_personal_info",
)
# A survey passes when the judge marks all of these "met".
CORE = ("advice_grounding", "measures_adoption", "no_new_advice", "no_personal_info")
CALIBRATION_THRESHOLD = 0.9
GENERATED = ("accepted", "rejected")
INSIGHT_FIELDS = (
    "query_summary", "advice_summary", "resolution_type", "asset", "concern",
    "action_window_days", "verification",
)

Verdict = Literal["met", "not_met", "not_applicable"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Criterion(_Strict):
    verdict: Verdict
    rationale: str = Field(max_length=400)


class RelevanceJudgment(_Strict):
    advice_grounding: Criterion
    measures_adoption: Criterion
    measures_outcome: Criterion
    follow_up_routing: Criterion
    no_new_advice: Criterion
    no_personal_info: Criterion
    reference_coverage: str = Field(max_length=600)


Judge = Callable[[str], Awaitable[RelevanceJudgment]]


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def reference_items(expected: Mapping) -> list[dict]:
    """Golden reference questions in the survey wire shape, without follow-ups."""
    return [
        {"prompt": item["text"], "options": [{"label": o} for o in item["options"]]}
        for item in expected.get("items") or []
    ]


def _items(items: list[Mapping]) -> list[dict]:
    out = []
    for item in items:
        entry: dict = {"question": item["prompt"]}
        if item.get("options"):
            entry["options"] = [
                {k: o[k] for k in ("label", "value", "follow_up") if o.get(k)}
                for o in item["options"]
            ]
        if "scale_min" in item:
            entry["scale"] = {
                k: item[k] for k in (
                    "scale_min", "scale_max", "follow_up_at_or_below", "follow_up_reason",
                ) if item.get(k) is not None
            }
        out.append(entry)
    return out


def render(insight: Mapping, survey_items: list[Mapping], expected: Mapping | None) -> str:
    """The judge's message. Farmer, conversation and location IDs are left out."""
    reference = expected.get("items") if expected else None
    return json.dumps({
        "insight": {key: insight.get(key) for key in INSIGHT_FIELDS},
        "survey": _items(survey_items),
        "reference": [
            {"question": item["text"], "options": item["options"]} for item in reference
        ] if reference else None,
    }, ensure_ascii=False, indent=1)


# ---- calibration controls (dev only) ---------------------------------------------

GENERIC_ITEM = {
    "prompt": "Was the FarmerChat app easy to use?",
    "options": [{"label": "Yes"}, {"label": "No"}],
}
NEW_ADVICE_ITEM = {
    "prompt": "Did you also spray neem oil every week, as we now suggest?",
    "options": [{"label": "Yes"}, {"label": "No"}],
}
PERSONAL_ITEM = {
    "prompt": "Please tell us your mobile number and your village name.",
    "options": [{"label": "Will share"}, {"label": "Prefer not"}],
}
# "Yes" is not here: "Have you seen new drooping plants? Yes" reports a problem.
POSITIVE_LABELS = {"Better", "All steps"}
PROBLEM_ROUTES = {"Worse": "advice_did_not_work", "Could not reach": "could_not_reach_expert"}


def _routed(items: list[dict], routes: Mapping[str, str]) -> list[dict]:
    items = copy.deepcopy(items)
    for item in items:
        for option in item["options"]:
            if option["label"] in routes:
                option["follow_up"] = routes[option["label"]]
    return items


def controls(rows: list[Mapping]) -> list[dict]:
    """Known-answer controls for each dev row that expects a survey.

    Each control is {golden_id, control, insight, items, expected, verdicts}, where
    `verdicts` maps only the criteria this control tests to the expected verdict.
    """
    surveyed = [row for row in rows if row["expected"]["survey"]]
    names = sorted({
        (row["insight"]["asset"].get("name") or "").lower() for row in rows
    } - {""}, key=len, reverse=True)
    out = []
    for row in surveyed:
        insight, expected = row["insight"], row["expected"]
        own = reference_items(expected)

        def add(control: str, items: list[dict], verdicts: dict[str, str]) -> None:
            out.append({
                "golden_id": row["golden_id"], "control": control, "insight": insight,
                "items": items, "expected": expected, "verdicts": verdicts,
            })

        add("own_reference", own, {
            "advice_grounding": "met", "measures_adoption": "met",
            "no_new_advice": "met", "no_personal_info": "met",
        })
        if any(o["label"] in PROBLEM_ROUTES for item in own for o in item["options"]):
            add("problem_routing", _routed(own, PROBLEM_ROUTES), {"follow_up_routing": "met"})
        positive = {label: "advice_did_not_work" for label in POSITIVE_LABELS}
        if any(o["label"] in positive for item in own for o in item["options"]):
            add("positive_routing", _routed(own, positive), {"follow_up_routing": "not_met"})
        add("injected_new_advice", [*own, NEW_ADVICE_ITEM], {"no_new_advice": "not_met"})
        add("injected_personal_info", [*own, PERSONAL_ITEM], {"no_personal_info": "not_met"})
        add("generic_question", [GENERIC_ITEM], {
            "advice_grounding": "not_met", "measures_adoption": "not_met",
            "measures_outcome": "not_met",
        })
        advice = normalize_advice(insight["advice_summary"], names)
        other = next((
            o for o in surveyed
            if o["insight"]["resolution_type"] != insight["resolution_type"]
            and o["insight"]["asset"].get("name") != insight["asset"].get("name")
            and normalize_advice(o["insight"]["advice_summary"], names) != advice
        ), None)
        if other is not None:
            add("unrelated_reference", reference_items(other["expected"]),
                {"advice_grounding": "not_met"})
    return out


async def _judge_all(judge: Judge, messages: list[str], concurrency: int) -> list:
    gate = asyncio.Semaphore(concurrency)

    async def one(message: str):
        async with gate:
            try:
                return await judge(message)
            except Exception as error:  # Recorded per case; one failure keeps the rest.
                return error

    return await asyncio.gather(*(one(m) for m in messages))


def _measure(hits: int, total: int) -> Measurement:
    if not total:
        return NotAssessed(reason="No applicable cases; no numeric score.")
    return Assessed(numerator=hits, denominator=total)


def _provenance(split_path: Path, rubric: str, judge_model: str) -> dict:
    split, _, split_bytes = load_split(split_path)
    return {
        "split_version": split["version"],
        "split_sha256": _sha(split_bytes),
        "rubric_sha256": _sha(rubric.encode()),
        "judge_model": judge_model,
    }


async def calibrate(
    judge: Judge, *, split_path: Path, rubric: str, judge_model: str,
    max_cases: int | None = None, concurrency: int = 3,
) -> dict:
    """Judge agreement with known-answer controls built from dev rows only."""
    split, source, _ = load_split(split_path)
    rows = [source[key] for key in split["dev_ids"]]
    if max_cases is not None:
        keep = {row["golden_id"] for row in rows if row["expected"]["survey"]}
        keep = set(sorted(keep)[:max_cases])
        rows = [row for row in rows if not row["expected"]["survey"] or row["golden_id"] in keep]
    cases = controls(rows)
    results = await _judge_all(
        judge, [render(c["insight"], c["items"], c["expected"]) for c in cases], concurrency,
    )
    agree, total = defaultdict(int), defaultdict(int)
    by_control: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    disagreements, failures = [], []
    for case, result in zip(cases, results, strict=True):
        if isinstance(result, Exception):
            failures.append({"golden_id": case["golden_id"], "control": case["control"],
                             "error": type(result).__name__})
            continue
        for criterion, wanted in case["verdicts"].items():
            got = getattr(result, criterion)
            total[criterion] += 1
            by_control[case["control"]][1] += 1
            if got.verdict == wanted:
                agree[criterion] += 1
                by_control[case["control"]][0] += 1
            else:
                disagreements.append({
                    "golden_id": case["golden_id"], "control": case["control"],
                    "criterion": criterion, "expected": wanted, "got": got.verdict,
                    "rationale": got.rationale,
                })
    agreement = {c: _measure(agree[c], total[c]) for c in CRITERIA}
    passed = not failures and all(
        isinstance(m, Assessed) and m.numerator / m.denominator >= CALIBRATION_THRESHOLD
        for m in agreement.values()
    )
    return {
        "mode": "relevance_calibration",
        "part": "dev",
        **_provenance(split_path, rubric, judge_model),
        "threshold": CALIBRATION_THRESHOLD,
        "passed": passed,
        "dev_rows": len(rows),
        "dev_survey_rows": sum(row["expected"]["survey"] for row in rows),
        "controls": len(cases),
        "failed_calls": len(failures),
        "agreement": {k: v.model_dump() for k, v in agreement.items()},
        "by_control": {k: {"agree": a, "checks": n} for k, (a, n) in sorted(by_control.items())},
        "disagreements": disagreements,
        "failures": failures,
    }


def require_calibration(
    calibration: Mapping, *, split_path: Path, rubric: str, judge_model: str,
) -> None:
    expected = _provenance(split_path, rubric, judge_model)
    if calibration.get("mode") != "relevance_calibration" or calibration.get("part") != "dev":
        raise ValueError("calibration must be a dev relevance calibration report")
    stale = [k for k, v in expected.items() if calibration.get(k) != v]
    if stale:
        raise ValueError(f"calibration does not match this rubric, model or split: {stale}")
    if calibration.get("passed") is not True:
        raise ValueError("calibration did not pass; tune the rubric on dev before scoring test")


async def score(
    judge: Judge, results: list[dict], *, split_path: Path, part: Literal["dev", "test"],
    rubric: str, judge_model: str, calibration: Mapping | None = None,
    results_sha256: str | None = None, concurrency: int = 3,
) -> dict:
    """Judge the generated surveys in saved worker results for one split half."""
    if calibration is not None or part == "test":
        if calibration is None:
            raise ValueError("scoring the test half needs a passing dev calibration report")
        require_calibration(
            calibration, split_path=split_path, rubric=rubric, judge_model=judge_model,
        )
    split, source, _ = load_split(split_path)
    ids = split["dev_ids"] if part == "dev" else split["holdout_ids"]
    by_insight = {source[key]["insight"]["insight_id"]: source[key] for key in ids}
    outside = sorted({r["insight_id"] for r in results} - by_insight.keys())
    if outside:
        raise ValueError(f"results include insights outside the {part} half: {outside[:5]}")
    if len({r["insight_id"] for r in results}) != len(results):
        raise ValueError("results repeat an insight")
    judged, missing, unexpected = [], [], []
    for result in results:
        row = by_insight[result["insight_id"]]
        survey = result.get("survey") or result.get("candidate_survey")
        generated = result["status"] in GENERATED and survey is not None
        if row["expected"]["survey"] and generated:
            judged.append((row, result, survey))
        elif row["expected"]["survey"]:
            missing.append(row["golden_id"])
        elif generated:
            unexpected.append(row["golden_id"])
    outcomes = await _judge_all(judge, [
        render(row["insight"], survey["items"], row["expected"]) for row, _, survey in judged
    ], concurrency)
    met, applicable = defaultdict(int), defaultdict(int)
    core = {"pass": 0, "judged": 0}
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    cases, failures = [], []
    for (row, result, _), outcome in zip(judged, outcomes, strict=True):
        if isinstance(outcome, Exception):
            failures.append({"golden_id": row["golden_id"], "error": type(outcome).__name__})
            continue
        for criterion in CRITERIA:
            verdict = getattr(outcome, criterion).verdict
            if verdict != "not_applicable":
                applicable[criterion] += 1
                met[criterion] += verdict == "met"
        passes = all(getattr(outcome, c).verdict == "met" for c in CORE)
        core["judged"] += 1
        core["pass"] += passes
        bucket = by_type[row["insight"]["resolution_type"]]
        bucket[0] += passes
        bucket[1] += 1
        cases.append({
            "golden_id": row["golden_id"], "insight_id": result["insight_id"],
            "status": result["status"], "core_pass": passes,
            "judgment": outcome.model_dump(),
        })
    return {
        "mode": "relevance_judge",
        "part": part,
        **_provenance(split_path, rubric, judge_model),
        "results_sha256": results_sha256,
        "calibrated": calibration is not None,
        "counts": {
            "results": len(results), "judged": len(judged), "failed_calls": len(failures),
            "expected_survey_but_none": len(missing), "survey_but_none_expected": len(unexpected),
        },
        "core_pass": _measure(core["pass"], core["judged"]).model_dump(),
        "core_criteria": list(CORE),
        "criteria": {c: _measure(met[c], applicable[c]).model_dump() for c in CRITERIA},
        "core_pass_by_resolution_type": {
            k: _measure(p, n).model_dump() for k, (p, n) in sorted(by_type.items())
        },
        "expected_survey_but_none": missing,
        "survey_but_none_expected": unexpected,
        "failures": failures,
        "cases": cases,
        "notes": [
            "Golden reference questions are English evidence, not an answer key.",
            "Only rows where the golden set and the worker both have a survey are judged.",
            "An LLM judge does not replace Hindi-speaking MEL review.",
        ],
    }
