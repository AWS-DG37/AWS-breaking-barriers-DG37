"""English versions of saved surveys, for the console phone's language switch.

Saved runs keep only the survey in the farmer's language. When a reviewer opens a
saved survey, the console asks the run's own review model for an English version once
and stores it by survey content (store.translation / store.save_translation). The
English text is for reading in the console only; it never reaches a farmer.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from mel.config import StrictModel
from mel.costs import RunCosts

PROMPT = """You translate short farmer follow-up surveys into plain English for program staff.
You receive JSON: a language code and a list of questions, each with an item_id, a prompt
and its answer labels in order. Return every question once, with the same item_id, an
English prompt, and English labels in the same order and the same number. Translate the
meaning faithfully and simply. Do not add, drop, merge or explain anything, and do not
answer the questions. Keep crop, product and place names as they are."""


class ItemTranslation(StrictModel):
    item_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=600)
    labels: list[str] = Field(default_factory=list, max_length=10)


class SurveyTranslation(StrictModel):
    items: list[ItemTranslation] = Field(min_length=1, max_length=10)


class TranslationCosts(RunCosts):
    """Charges a translation to the run whose survey it translates, under that run's budget
    and the console spend cap, without ever stopping the run. A refused or unsettled
    translation must not block later translations or rewrite a finished run's stop reason."""

    def stop(self, reason):
        pass


def survey_key(survey: dict) -> str:
    """SHA-256 of the survey's language and items: equal surveys share one translation."""
    body = json.dumps(
        {"language": survey.get("language"), "items": survey["items"]},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def request_text(survey: dict) -> str:
    questions = [
        {
            "item_id": item["item_id"],
            "prompt": item["prompt"],
            "labels": [option["label"] for option in item.get("options") or []],
        }
        for item in survey["items"]
    ]
    return json.dumps({"language": survey.get("language"), "questions": questions},
                      ensure_ascii=False)


def english_items(survey: dict, translation: SurveyTranslation) -> list[dict]:
    """The survey's items with English prompts and labels. Item ids, types, answer values,
    follow-ups and scales stay the survey's own, so answers mean the same in both languages.
    Raises ValueError when the translation does not match the survey question for question."""
    by_id = {entry.item_id: entry for entry in translation.items}
    if len(by_id) != len(translation.items):
        raise ValueError("the translation repeats a question")
    items = []
    for item in survey["items"]:
        entry = by_id.pop(item["item_id"], None)
        if entry is None:
            raise ValueError(f"the translation is missing {item['item_id']}")
        options = item.get("options")
        if len(entry.labels) != len(options or []):
            raise ValueError(f"the translation of {item['item_id']} has a different answer count")
        english = {**item, "prompt": entry.prompt.strip()}
        if options:
            english["options"] = [
                {**option, "label": label.strip()} for option, label in zip(options, entry.labels)
            ]
        items.append(english)
    if by_id:
        raise ValueError("the translation has questions the survey does not")
    return items
