"""Deterministic rules from data/pack/json/rules/rules.json: survey decision, format, language, timing."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from functools import cache

from pydantic import ValidationError

from .models import PACK, Insight, Survey

PII_PATTERNS = re.compile(r"\b(phone|mobile number|aadhaar|address|bank|account number|income)\b", re.I)
ADVICE_PATTERNS = re.compile(r"\b(you should|we recommend|try spraying|apply|use this)\b", re.I)
MIN_ITEMS, MAX_ITEMS = 1, 3
MAX_PROMPT, MAX_LABEL = 400, 20
MIN_OPTIONS, MAX_OPTIONS = 2, 10


@cache
def rules() -> dict:
    return json.loads((PACK / "json" / "rules" / "rules.json").read_text())


def _hhmm(s: str) -> time:
    return time.fromisoformat(s)


def decide(insight: Insight | dict, *, use_verification: bool = True, use_season_end: bool = True) -> tuple[bool, str]:
    """Survey or not, with a reason code.

    Order: junk -> no; resolution_type per rules.survey_by_resolution; then (optional)
    verification.needed false -> no; earliest window start after season_end -> no.
    """
    if isinstance(insight, dict):
        if insight.get("quality_flag") == "junk":
            return False, "junk_insight"
        try:
            insight = Insight(**insight)
        except ValidationError:  # broken rows never get a survey
            return False, "junk_insight"
    i = insight
    if i.quality_flag == "junk":
        return False, "junk_insight"
    if not rules()["survey_by_resolution"][i.resolution_type]["survey"]:
        return False, i.resolution_type
    if use_verification and not i.verification.needed:
        return False, "no_check_needed"
    if use_season_end:
        wr = rules()["window_rules"]
        aw = i.action_window_days if i.action_window_days is not None else wr["action_window_days_if_null"]
        start = i.created_at.date() + timedelta(days=max(wr["earliest_start_day"], aw))
        if start > date.fromisoformat(wr["season_end"]):
            return False, "window_after_season"
    return True, "survey"


def in_send_hours(dt: datetime) -> bool:
    h = rules()["send_hours"]
    return _hhmm(h["from"]) <= dt.timetz().replace(tzinfo=None) <= _hhmm(h["to"])


def check_survey(survey: Survey, insights: dict[str, Insight]) -> dict[str, bool]:
    src = [insights[i] for i in survey.source_insight_ids if i in insights]
    opts = [o for it in survey.items for o in (it.options or [])]
    w = survey.send_window
    res = {
        "sources_exist": len(src) == len(survey.source_insight_ids),
        "same_farmer": all(s.farmer_id == survey.farmer_id for s in src),
        "sources_want_survey": all(decide(s)[0] for s in src),
        "item_count_ok": MIN_ITEMS <= len(survey.items) <= MAX_ITEMS,
        "prompt_length_ok": all(len(it.prompt) <= MAX_PROMPT for it in survey.items),
        "label_length_ok": all(len(o.label) <= MAX_LABEL for o in opts),
        "option_count_ok": all(
            MIN_OPTIONS <= len(it.options or []) <= MAX_OPTIONS for it in survey.items if it.type != "survey-scale"
        ),
        "language_ok": all(s.conversation_language == survey.language for s in src),
        "window_ordered": w.not_before < w.not_after,
        "send_hours_ok": in_send_hours(w.not_before) and in_send_hours(w.not_after),
        "before_season_end": w.not_after.date() <= date.fromisoformat(rules()["window_rules"]["season_end"]),
        "no_pii": not any(PII_PATTERNS.search(it.prompt) for it in survey.items),
        "no_new_advice": not any(ADVICE_PATTERNS.search(it.prompt) for it in survey.items),
    }
    if src:
        wr = rules()["window_rules"]
        aw = lambda s: s.action_window_days if s.action_window_days is not None else wr["action_window_days_if_null"]
        earliest = max(s.created_at.date() + timedelta(days=max(wr["earliest_start_day"], aw(s))) for s in src)
        res["after_action_window"] = w.not_before.date() >= earliest
    return res


def passed(results: dict[str, bool]) -> bool:
    return all(results.values())
