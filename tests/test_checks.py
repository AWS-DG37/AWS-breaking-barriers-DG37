import json
from datetime import timedelta

import pytest

from dg_agent.checks import check_survey, decide, passed
from dg_agent.models import PACK, Insight, Survey

SC = PACK / "json" / "scenarios" / "1-mustard-aphid-spray"


@pytest.fixture
def case():
    ins = Insight(**json.loads((SC / "insight.json").read_text()))
    lang = ins.conversation_language
    survey = Survey(**json.loads((SC / f"survey.{lang}.json").read_text()))
    return {ins.insight_id: ins}, survey


def test_worked_survey_passes(case):
    insights, s = case
    assert passed(check_survey(s, insights)), check_survey(s, insights)


def test_wrong_language_fails(case):
    insights, s = case
    s.language = "en-IN" if s.language != "en-IN" else "hi-IN"
    assert not check_survey(s, insights)["language_ok"]


def test_too_early_fails(case):
    insights, s = case
    s.send_window.not_before = next(iter(insights.values())).created_at
    assert not check_survey(s, insights)["after_action_window"]


def test_outside_send_hours_fails(case):
    insights, s = case
    s.send_window.not_after = s.send_window.not_after.replace(hour=20)
    assert not check_survey(s, insights)["send_hours_ok"]


def test_long_label_and_prompt_fail(case):
    insights, s = case
    s.items[0].options[0].label = "x" * 21
    s.items[0].prompt = "x" * 401
    r = check_survey(s, insights)
    assert not r["label_length_ok"] and not r["prompt_length_ok"]


def test_too_many_items_fails(case):
    insights, s = case
    s.items = s.items * 2
    assert not check_survey(s, insights)["item_count_ok"]


def test_pii_fails(case):
    insights, s = case
    s.items[0].prompt = "What is your phone number?"
    assert not check_survey(s, insights)["no_pii"]


def test_decision_rules():
    base = json.loads((SC / "insight.json").read_text())
    assert decide(base)[0]
    assert decide({**base, "quality_flag": "junk"}) == (False, "junk_insight")
    assert decide({**base, "resolution_type": "generic_information"}) == (False, "generic_information")
    assert decide({**base, "resolution_type": "needs_more_context"}) == (False, "needs_more_context")
