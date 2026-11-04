"""Closed draft answers and evidence for personal-information rejections."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mel.models import Review, SurveyDraft
from mel.survey_checks import survey_issues

GOLDEN = Path(__file__).parents[1] / "docs/participant-pack/data/golden_set.jsonl"
LABEL_CODES = {
    "Yes": "yes",
    "No": "no",
    "Not yet": "not_yet",
    "Better": "better",
    "Same": "same",
    "Worse": "worse",
    "All steps": "all_steps",
    "Some steps": "some_steps",
    "Not started": "not_started",
    "Too early to say": "too_early",
    "Did not check": "did_not_check",
    "Could not reach": "could_not_reach",
    "Not met yet": "not_met_yet",
    "Too costly": "too_costly",
    "Not available nearby": "not_available",
    "No time": "no_time",
    "Did not understand": "did_not_understand",
}


def test_every_golden_survey_validates_with_closed_answer_codes():
    seen = set()
    count = 0
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if not row["expected"]["survey"]:
            continue
        items = []
        for item in row["expected"]["items"]:
            options = []
            for option in item["options"]:
                label = option["label"] if isinstance(option, dict) else option
                options.append({"label": label, "value": LABEL_CODES[label]})
                seen.add(label)
            items.append(
                {
                    "item_id": item["id"],
                    "type": "survey-single-select",
                    "prompt": item["text"],
                    "options": options,
                }
            )
        SurveyDraft.model_validate({"rationale": row["golden_id"], "items": items})
        count += 1
    assert count == 95
    assert len(seen) == 17
    assert seen == LABEL_CODES.keys()


@pytest.fixture
def draft():
    return {
        "rationale": "Measure the advised action.",
        "items": [
            {
                "item_id": "q1",
                "type": "survey-single-select",
                "prompt": "क्या आपने छिड़काव किया?",
                "options": [
                    {"value": "yes", "label": "हाँ"},
                    {"value": "no", "label": "नहीं"},
                ],
            },
            {
                "item_id": "q2",
                "type": "survey-single-select",
                "prompt": "अब फसल कैसी है?",
                "options": [
                    {"value": "better", "label": "बेहतर"},
                    {"value": "same", "label": "पहले जैसी"},
                    {"value": "worse", "label": "खराब", "follow_up": "crop_or_animal_damaged"},
                ],
            },
        ],
    }


@pytest.mark.parametrize("trap", ["dont_know", "based_on", "phone_expert"])
def test_benign_wording_passes_draft_and_routable_checks(draft, trap):
    language = "hi-IN"
    item = draft["items"][0]
    if trap == "dont_know":
        item["options"].append({"value": "cannot_tell", "label": "पता नहीं"})
    elif trap == "based_on":
        item["prompt"] = "क्या आपने सलाह पर आधारित छिड़काव किया?"
    else:
        language = "en-IN"
        draft["items"] = [item]
        item["prompt"] = "Did you phone the expert?"
        item["options"] = [
            {"value": "yes", "label": "Yes"},
            {"value": "not_yet", "label": "Not yet"},
            {
                "value": "could_not_reach",
                "label": "Could not reach",
                "follow_up": "could_not_reach_expert",
            },
        ]
    validated = SurveyDraft.model_validate(draft)
    assert survey_issues({"language": language, **validated.model_dump(exclude_none=True)}) == []


def test_invented_income_answer_code_is_rejected(draft):
    draft["items"][0]["options"][0]["value"] = "below_1_lakh"
    with pytest.raises(ValidationError, match="items.0.options.0.value"):
        SurveyDraft.model_validate(draft)


@pytest.fixture
def review():
    return {
        "relevance_score": 0.95,
        "language_correct": True,
        "gives_new_advice": False,
        "asks_personal_info": True,
        "issues": [],
    }


@pytest.mark.parametrize("quote", [None, "", " \n\t"])
def test_personal_information_flag_requires_nonblank_quote(review, quote):
    review["personal_info_quote"] = quote
    with pytest.raises(ValidationError, match="personal_info_quote"):
        Review.model_validate(review)


def test_personal_information_flag_requires_quote_when_omitted(review):
    with pytest.raises(ValidationError, match="personal_info_quote"):
        Review.model_validate(review)


def test_personal_information_flag_accepts_exact_quote(review):
    quote = "क्या आपके पास बैंक खाता है?"
    validated = Review.model_validate({**review, "personal_info_quote": quote})
    assert validated.personal_info_quote == quote


def test_nonpersonal_review_defaults_quote_to_none(review):
    validated = Review.model_validate({**review, "asks_personal_info": False})
    assert validated.personal_info_quote is None
