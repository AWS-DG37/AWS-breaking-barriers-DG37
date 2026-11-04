"""Deterministic survey checks: catch known defects, pass every known-good survey."""

import copy
import json
from pathlib import Path

import pytest

from mel.survey_checks import survey_issues

SCENARIOS = sorted(Path("docs/participant-pack/json/scenarios").glob("*/survey.*.json"))
GOLDEN = Path("docs/participant-pack/data/golden_set.jsonl")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_pack_survey_passes(path):
    assert survey_issues(load(path)) == []


def test_golden_reference_questions_raise_no_false_positives():
    rows = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]
    flagged = []
    for row in rows:
        for item in row["expected"].get("items", []):
            labels = [o["label"] if isinstance(o, dict) else o for o in item["options"]]
            options = [{"label": label} for label in labels]
            options[0]["follow_up"] = "advice_did_not_work"
            survey = {
                "language": "en-IN",
                "items": [{"item_id": "q1", "prompt": item["text"], "options": options}],
            }
            flagged += survey_issues(survey)
    assert SCENARIOS and rows
    assert flagged == []


def test_dont_know_answers_are_not_personal_information():
    survey = load(next(p for p in SCENARIOS if p.name == "survey.hi-IN.json"))
    survey["items"][0]["options"].append({"value": "dont_know", "label": "पता नहीं"})
    assert survey_issues(survey) == []


def drop_follow_ups(survey):
    for item in survey["items"]:
        item.pop("follow_up_reason", None)
        for option in item.get("options", []):
            option.pop("follow_up", None)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: s["items"][0].update(prompt=s["items"][0]["prompt"] + " \U0001f33e"), "emoji"),
        (lambda s: s["items"][0].update(prompt="Did you spray on 12/11?"), "exact date"),
        (lambda s: s["items"][0].update(prompt="What is your phone number?"), "personal"),
        (lambda s: s.update(language="en-IN"), "Devanagari in an English"),
        (lambda s: s["items"][0].update(prompt="Did you spray?"), "no Devanagari"),
        (drop_follow_ups, "routed back"),
    ],
)
def test_injected_defects_are_caught(change, message):
    survey = copy.deepcopy(load(next(p for p in SCENARIOS if p.name == "survey.hi-IN.json")))
    change(survey)
    assert any(message in issue for issue in survey_issues(survey))
