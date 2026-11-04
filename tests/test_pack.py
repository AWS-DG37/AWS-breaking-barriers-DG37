"""The participant pack loads and validates against both the JSON Schemas and our models."""
import csv
import json
from pathlib import Path

import pytest

from dg_agent.checks import decide

from dg_agent.models import (PACK, BottomSheet, Farmer, Insight, NoSurvey, Response,
                           ScheduleHandoff, Survey, validate)
from conftest import OUTCOMES, SCENARIOS, jsonl

DATA = PACK / "data"
load = lambda p: json.loads(Path(p).read_text())


def test_every_insight_row_loads():
    rows = jsonl(DATA / "insights.jsonl")
    assert len(rows) == 6999
    bad, junk_no_survey = [], 0
    for r in rows:
        if r["quality_flag"] == "junk":  # broken on purpose: must load and get no survey
            junk_no_survey += decide(r) == (False, "junk_insight")
            continue
        Insight(**r)
        payload = {k: v for k, v in r.items() if k != "quality_flag"}  # practice-only label
        if errs := validate("insight", payload):
            bad.append((r["insight_id"], errs[:1]))
    assert not bad, bad[:5]
    assert junk_no_survey == sum(r["quality_flag"] == "junk" for r in rows) == 723


def test_farmers_load():
    farmers = [Farmer.from_csv_row(r) for r in csv.DictReader((DATA / "farmers.csv").open())]
    assert len(farmers) == 300


def test_six_scenarios():
    assert len(SCENARIOS) == 6


@pytest.mark.parametrize("d", SCENARIOS, ids=lambda d: d.name)
def test_scenario_validates(d: Path):
    ins = load(d / "insight.json")
    assert not validate("insight", ins)
    Insight(**ins)
    if (d / "no_survey.json").exists():
        ns = load(d / "no_survey.json")
        assert not validate("no_survey", ns)
        NoSurvey(**ns)
        return
    for f in d.glob("survey.*.json"):
        s = load(f)
        assert not validate("survey", s), f.name
        Survey(**s)
    h = load(d / "schedule_handoff.json")
    assert not validate("schedule_handoff", h)
    ScheduleHandoff(**h)
    for f in (d / "responses").glob("*.json"):
        r = load(f)
        assert not validate("response", r), f.name
        Response(**r)
    for f in (d / "render").rglob("*.json"):
        if f.stem in ("app", "web"):
            r = load(f)
            sheet = {"presentation": r["presentation"], "sheet": r["sheet"]}
            assert not validate("bottom_sheet", sheet), f
            BottomSheet(**sheet)


def test_outcome_count():
    assert len(OUTCOMES) == 183


@pytest.mark.parametrize("f", OUTCOMES, ids=lambda f: "/".join(f.parts[-3:]))
def test_outcome_validates(f: Path):
    r = load(f)
    assert not validate("response", r)
    Response(**r)
