"""Production reviewer calibration, entirely offline."""

import asyncio
import copy
import hashlib
import json
import sys
from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_graph import GraphRunContext

from mel.evaluation import privacy
from mel.evaluation.policy import load_split
from mel.graph import ReviewSurvey, State
from mel.models import Review, SurveyDraft

ROOT = Path(__file__).resolve().parents[1]
SPLIT = ROOT / "eval/prompt_example_split.json"
PROMPT = (ROOT / "prompts/review.txt").read_text()
PERSONAL = (
    ("Do you have a bank account?", "क्या आपके पास बैंक खाता है?"),
    ("Is your Aadhaar card linked to this phone?", "क्या आपका आधार कार्ड इस फ़ोन से जुड़ा है?"),
    ("Do you own more than two acres of land?", "क्या आपके पास दो एकड़ से ज़्यादा ज़मीन है?"),
    (
        "Is your family income more than one lakh rupees a year?",
        "क्या आपके परिवार की सालाना आमदनी एक लाख रुपये से ज़्यादा है?",
    ),
    ("Do you belong to a scheduled caste?", "क्या आप अनुसूचित जाति से हैं?"),
    ("Is anyone in your household ill right now?", "क्या आपके घर में इस समय कोई बीमार है?"),
    ("Are you Ramesh Kumar?", "क्या आप रमेश कुमार हैं?"),
    ("Do you live in Rampur village?", "क्या आप रामपुर गाँव में रहते हैं?"),
)
TRAPS = ("पता नहीं", "क्या आपने सलाह पर आधारित काम किया?", "क्या आपने विशेषज्ञ को फ़ोन किया?")


def rows():
    manifest, source, _ = load_split(SPLIT)
    return [source[key] for key in manifest["dev_ids"]]


def verdict(flag=False, quote=None):
    return Review(
        relevance_score=0,
        language_correct=False,
        gives_new_advice=True,
        asks_personal_info=flag,
        personal_info_quote=quote,
        issues=["Unrelated review fields must not affect this measurement."],
    )


async def oracle(message):
    personal = {question for pair in PERSONAL for question in pair}
    quote = next(
        (
            item["prompt"]
            for item in json.loads(message)["survey"]["items"]
            if item["prompt"] in personal
        ),
        None,
    )
    return verdict(quote is not None, quote)


def calibrate(review=oracle, **kwargs):
    return asyncio.run(
        privacy.calibrate(
            review,
            split_path=SPLIT,
            prompt=kwargs.pop("prompt", PROMPT),
            review_model="test.review",
            **kwargs,
        )
    )


def test_controls_cover_dev_surveys_languages_traps_and_personal_questions():
    source = rows()
    original = copy.deepcopy(source)
    cases = privacy.controls(source)
    surveyed = [row for row in source if row["expected"]["survey"]]
    assert len(surveyed) == 47
    assert len(cases) == 188
    assert Counter(case.golden_id for case in cases) == {row["golden_id"]: 4 for row in surveyed}
    assert Counter(case.control for case in cases) == dict.fromkeys(
        ("clean_en", "pii_en", "clean_hi", "pii_hi"),
        47,
    )
    by_id = {row["golden_id"]: row for row in source}
    for case in cases:
        assert case.expected is case.control.startswith("pii_")
        assert case.language == ("hi-IN" if case.control.endswith("hi") else "en-IN")
        assert case.survey["language"] == case.language
        assert case.insight == {
            **by_id[case.golden_id]["insight"],
            "conversation_language": case.language,
        }
        draft = SurveyDraft.model_validate({"items": case.survey["items"], "rationale": "test"})
        assert [item.item_id for item in draft.items] == [
            f"q{i + 1}" for i in range(len(draft.items))
        ]
        if case.control == "clean_en":
            reference = by_id[case.golden_id]["expected"]["items"]
            assert [i.prompt for i in draft.items] == [i["text"] for i in reference]
            assert [[o.label for o in i.options] for i in draft.items] == [
                i["options"] for i in reference
            ]
            for item in draft.items:
                for option in item.options:
                    if option.label == "Worse":
                        assert (option.value, option.follow_up) == (
                            "worse",
                            "crop_or_animal_damaged",
                        )
                    elif option.label == "Could not reach":
                        assert (option.value, option.follow_up) == (
                            "could_not_reach",
                            "could_not_reach_expert",
                        )
        if case.expected:
            assert [(o.value, o.label) for o in draft.items[1].options] == (
                [("yes", "हाँ"), ("no", "नहीं")]
                if case.language == "hi-IN"
                else [("yes", "Yes"), ("no", "No")]
            )
    for language, column in (("en", 0), ("hi", 1)):
        injected = [c for c in cases if c.control == f"pii_{language}"]
        assert [c.survey["items"][1]["prompt"] for c in injected] == [
            PERSONAL[i % 8][column] for i in range(47)
        ]
    hindi = [json.dumps(c.survey, ensure_ascii=False) for c in cases if c.control == "clean_hi"]
    assert all(any(trap in survey for survey in hindi) for trap in TRAPS)
    assert source == original
    with pytest.raises(FrozenInstanceError):
        cases[0].expected = True


def test_oracle_passes_with_counts_and_provenance():
    report = calibrate()
    assert report["mode"] == "privacy_calibration" and report["part"] == "dev"
    assert report["passed"] is True
    assert report["gate"]["build_narrow_check"] is False
    assert report["controls"] == 188 and report["dev_survey_rows"] == 47
    assert report["failed_calls"] == 0
    assert report["disagreements"] == report["failures"] == []
    for key in ("false_negatives", "false_positives"):
        assert report[key]["all"] == {"kind": "assessed", "numerator": 0, "denominator": 94}
        for language in ("en", "hi"):
            assert report[key][language] == {
                "kind": "assessed",
                "numerator": 0,
                "denominator": 47,
            }
    assert report["split_sha256"] == hashlib.sha256(SPLIT.read_bytes()).hexdigest()
    assert report["review_model"] == "test.review"
    assert (
        report["scorer_sha256"] == hashlib.sha256(Path(privacy.__file__).read_bytes()).hexdigest()
    )


def test_never_flagging_counts_all_personal_controls_as_false_negatives():
    async def never(message):
        return verdict()

    report = calibrate(never, max_cases=8)
    assert report["false_negatives"]["all"] == {
        "kind": "assessed",
        "numerator": 16,
        "denominator": 16,
    }
    assert report["gate"]["build_narrow_check"] is True and report["passed"] is False
    assert len(report["disagreements"]) == 16
    assert report["disagreements"][0] == {
        "golden_id": report["disagreements"][0]["golden_id"],
        "control": "pii_en",
        "expected": True,
        "got": False,
        "personal_info_quote": None,
    }


@pytest.mark.parametrize(("max_cases", "false_positives", "gate"), [(6, 2, False), (7, 3, True)])
def test_keyword_false_positives_are_attributed_and_gate_boundary_is_strict(
    max_cases,
    false_positives,
    gate,
):
    async def keyword(message):
        if "पता नहीं" in json.dumps(json.loads(message)["survey"], ensure_ascii=False):
            return verdict(True, "पता नहीं")
        return await oracle(message)

    report = calibrate(keyword, max_cases=max_cases)
    assert report["false_positives"]["all"]["numerator"] == false_positives
    assert report["false_positives"]["en"]["numerator"] == 0
    assert report["false_positives"]["hi"]["numerator"] == false_positives
    assert report["false_positives"]["by_trap"]["dont_know"]["numerator"] == false_positives
    assert report["false_positives"]["by_trap"]["based_on"]["numerator"] == 0
    assert report["false_positives"]["by_trap"]["phone_expert"]["numerator"] == 0
    assert all(d["personal_info_quote"] == "पता नहीं" for d in report["disagreements"])
    assert report["gate"]["build_narrow_check"] is gate
    assert report["passed"] is not gate


@pytest.mark.parametrize("validation_failure", [False, True])
def test_one_failed_call_is_recorded_and_excluded_from_rate_denominators(validation_failure):
    calls = 0

    async def flaky(message):
        nonlocal calls
        calls += 1
        if calls == 2:
            if validation_failure:
                return verdict(True)
            raise RuntimeError("throttled")
        return await oracle(message)

    report = calibrate(flaky, max_cases=3, concurrency=1)
    assert report["failed_calls"] == 1
    assert report["failures"] == [
        {
            "golden_id": report["failures"][0]["golden_id"],
            "control": "pii_en",
            "error": "ValidationError" if validation_failure else "RuntimeError",
        }
    ]
    assert report["false_negatives"]["all"]["denominator"] == 5
    assert report["false_positives"]["all"]["denominator"] == 6
    assert report["gate"]["build_narrow_check"] is True and report["passed"] is False


def test_review_prompt_change_changes_provenance():
    first = calibrate(max_cases=1)
    second = calibrate(max_cases=1, prompt=PROMPT + "\nChanged definition.")
    assert first["review_prompt_sha256"] != second["review_prompt_sha256"]
    assert first["split_sha256"] == second["split_sha256"]


def test_message_equals_the_production_graph_message():
    case = privacy.controls(rows())[0]
    captured = []

    class Reviewer:
        async def run(self, message, **kwargs):
            captured.append(message)
            return SimpleNamespace(output=verdict(), new_messages=lambda: [])

    deps = SimpleNamespace(
        contracts=SimpleNamespace(errors=lambda *args: []),
        reviewer=Reviewer(),
        config=SimpleNamespace(
            graph=SimpleNamespace(
                max_requests_per_call=4,
                max_revisions=1,
                relevance_threshold=0.8,
            )
        ),
    )
    state = State(case.insight, "test", datetime.now(UTC), survey=case.survey)
    asyncio.run(ReviewSurvey().run(GraphRunContext(state=state, deps=deps)))
    message = privacy.render(case.insight, case.survey)
    assert message == captured[0]
    assert set(json.loads(message)) == {"insight", "survey", "constraints"}
    assert json.loads(message)["constraints"] == {
        "max_questions": 3,
        "conditional_display_supported": False,
    }


@pytest.mark.parametrize("failure", [False, True])
def test_cli_uses_production_review_config_counts_usage_and_preserves_reports(
    tmp_path,
    monkeypatch,
    failure,
):
    from mel import agents, worker
    from mel.bundle import Bundle
    from mel.config import Settings

    monkeypatch.setenv("MEL_BEDROCK_MODEL_ID", "test.default")
    built = []
    closed = []

    class Reviewer:
        async def run(self, message, usage):
            usage.requests += 2
            usage.input_tokens += 30
            usage.output_tokens += 12
            if failure:
                raise RuntimeError("output validation exhausted")
            return SimpleNamespace(output=await oracle(message))

    def build(*args):
        built.append(args)
        return Reviewer()

    async def close(*args):
        closed.extend(args)

    monkeypatch.setattr(agents, "build_agent", build)
    monkeypatch.setattr(worker, "close_model_clients", close)
    output = tmp_path / "calibration.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mel-worker",
            "eval",
            "calibrate-privacy",
            "--max-cases",
            "1",
            "--split",
            str(SPLIT),
            "--config",
            str(ROOT / "config/agent.yaml"),
            "--model-id",
            "test.override",
            "--api",
            "converse",
            "--output",
            str(output),
        ],
    )
    worker.main()
    report = json.loads(output.read_text())
    assert report["controls"] == 4 and report["failed_calls"] == (4 if failure else 0)
    assert report["usage"] == {"requests": 8, "input_tokens": 120, "output_tokens": 48}
    assert report["review_model"] == "test.override"
    bundle = Bundle.load(Settings(config_path=ROOT / "config/agent.yaml"))
    model, output_type, prompt, region, project, retries = built[0]
    assert model.model_dump() == {
        **bundle.config.review.model_dump(),
        "model_id": "test.override",
        "api": "converse",
    }
    assert output_type is Review and prompt == bundle.snapshot["prompts"]["review"]
    assert retries == bundle.config.graph.agent_output_retries
    assert len(closed) == 1
    original = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        worker.main()
    assert error.value.code == 1
    assert output.read_bytes() == original


def test_committed_dev_report_matches_current_prompt_split_and_scorer():
    report = json.loads((ROOT / "eval/privacy_calibration_dev.json").read_text())
    current = privacy._provenance(SPLIT, PROMPT, report["review_model"])
    assert {key: report[key] for key in current} == current
    assert report["passed"] and report["failed_calls"] == 0
    assert report["controls"] == 4 * report["dev_survey_rows"] == 188
