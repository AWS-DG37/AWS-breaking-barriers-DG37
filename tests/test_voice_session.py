"""The voice page reads each question aloud and turns taps into valid answer records.

The state module is plain JavaScript (docs/voice/survey-session.mjs) so the static page
can import it. These tests drive it through Node and validate what it produces against
the participant pack's schemas, and check that every spoken line has a recording.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mel.contracts import Contracts

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "docs/voice/survey-session.mjs"
SCENARIOS = ROOT / "docs/participant-pack/json/scenarios"
CONTRACTS = Contracts(ROOT / "docs/participant-pack/json/schemas")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = """
import {createSession, recordAnswer, markShown, finishSurvey, spokenLines, clipName} from %s;
const {survey, steps, finish} = JSON.parse(process.argv[1]);
const now = new Date('2026-12-07T04:00:00Z');
const session = createSession(survey, {now});
const replies = steps.map(step => step.shown
  ? (markShown(session, step.shown), null)
  : recordAnswer(session, {...step, now}));
const record = finishSurvey(session, {...finish, now});
const lines = spokenLines(survey);
const clip = await clipName(survey.language, lines.opening);
const out = {replies, record, lines, clip};
console.log(JSON.stringify(out));
"""


def run(scenario: str, language: str, steps: list[dict], finish: dict | None = None) -> dict:
    survey = json.loads((SCENARIOS / scenario / f"survey.{language}.json").read_text())
    payload = json.dumps({"survey": survey, "steps": steps, "finish": finish or {}})
    out = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS % json.dumps(MODULE.as_uri()), payload],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def assert_valid(record: dict) -> None:
    assert CONTRACTS.errors("response", record) == []


def test_taps_on_every_question_make_a_completed_record():
    result = run(
        "1-mustard-aphid-spray",
        "en-IN",
        [
            {"item_id": "q1", "value": "yes", "heard": "Yes"},
            {"item_id": "q2", "value": "easy", "heard": "Yes, easily"},
            {"item_id": "q3", "value": "fewer", "heard": "Fewer than before"},
        ],
    )
    record = result["record"]
    assert_valid(record)
    assert record["status"] == "completed"
    assert record["complete"] is True
    assert record["answers"] == {"q1": "yes", "q2": "easy", "q3": "fewer"}
    assert {i["input_kind"] for i in record["items"]} == {"sheet_tap"}
    assert [r["next_item_id"] for r in result["replies"]] == ["q2", "q3", None]


def test_an_answer_that_means_the_advice_failed_is_routed_for_follow_up():
    result = run(
        "1-mustard-aphid-spray",
        "en-IN",
        [
            {"item_id": "q1", "value": "yes", "heard": "yes"},
            {"item_id": "q2", "value": "easy", "heard": "easy"},
            {"item_id": "q3", "value": "more", "heard": "there are more insects"},
        ],
    )
    record = result["record"]
    assert_valid(record)
    assert result["replies"][2]["follow_up"] == "advice_did_not_work"
    assert record["status"] == "negative_outcome"
    assert record["needs_follow_up"] is True
    assert record["follow_up_reasons"] == ["advice_did_not_work"]


def test_a_value_that_is_not_a_choice_is_refused_with_the_valid_choices():
    result = run(
        "1-mustard-aphid-spray",
        "en-IN",
        [
            {"item_id": "q1", "value": "maybe", "heard": "maybe"},
            {"item_id": "q1", "value": "no", "heard": "no, not yet"},
        ],
    )
    refused, accepted = result["replies"]
    assert refused["accepted"] is False
    assert "yes" in refused["reason"] and "no" in refused["reason"]
    assert accepted["accepted"] is True
    record = result["record"]
    assert_valid(record)
    assert record["status"] == "partial"
    q1 = record["items"][0]
    assert (q1["result"], q1["value"], q1["attempts"]) == ("answered", "no", 2)
    assert [i["result"] for i in record["items"][1:]] == ["not_reached", "not_reached"]


def test_a_tapped_scale_number_is_stored_as_an_integer_and_a_low_score_needs_follow_up():
    result = run(
        "3-potato-blight-wait",
        "en-IN",
        [
            {"item_id": "q1", "value": "yes", "heard": "yes"},
            {"shown": "q2"},
            {"item_id": "q2", "value": "skipped", "heard": "I don't know"},
            {"item_id": "q3", "value": "1", "heard": "one"},
        ],
    )
    record = result["record"]
    assert_valid(record)
    assert record["answers"]["q3"] == 1
    assert record["items"][1]["result"] == "skipped"
    assert record["follow_up_reasons"] != []
    assert "from 1 to 5" in result["lines"]["items"]["q3"]


def test_a_tap_on_screen_is_recorded_as_a_sheet_tap_in_hindi():
    result = run(
        "1-mustard-aphid-spray",
        "hi-IN",
        [
            {"item_id": "q1", "value": "yes", "heard": "हाँ", "input_kind": "sheet_tap"},
        ],
    )
    record = result["record"]
    assert_valid(record)
    assert record["language"] == "hi-IN"
    assert record["items"][0]["input_kind"] == "sheet_tap"


def test_a_sheet_closed_with_no_answer_is_recorded_as_dismissed():
    record = run("2-wheat-irrigation-timing", "en-IN", [{"shown": "q1"}])["record"]
    assert_valid(record)
    assert (record["status"], record["failure_code"]) == ("declined", "sheet_dismissed")
    assert (record["sheet_outcome"], record["retry_decision"]) == ("dismissed", "give_up")
    assert record["answers"] == {}


OUTCOMES = json.loads((ROOT / "docs/participant-pack/json/rules/outcome_codes.json").read_text())


def test_every_web_record_follows_the_pack_outcome_rules():
    cases = {
        "dismissed": [{"shown": "q1"}],
        "partway": [{"item_id": "q1", "value": "yes", "heard": "Yes"}],
        "finished": [
            {"item_id": "q1", "value": "yes", "heard": "Yes"},
            {"item_id": "q2", "value": "easy", "heard": "Yes, easily"},
            {"item_id": "q3", "value": "fewer", "heard": "Fewer than before"},
        ],
    }
    for name, steps in cases.items():
        record = run("1-mustard-aphid-spray", "en-IN", steps)["record"]
        assert_valid(record)
        assert record["channel"] == "web"
        code = record["failure_code"]
        if code:
            rule = OUTCOMES["failures"][code]
            assert "web" in rule["channels"], (name, code)
        elif record["sheet_outcome"] == "closed_partway":
            rule = OUTCOMES["sheet_cases"]["sheet_closed_partway"]
        else:
            rule = {"retry_decision": "none", "counts_toward_cap": True}
        assert record["retry_decision"] == rule["retry_decision"], name
        assert record["counts_toward_cap"] == rule["counts_toward_cap"], name
        if "status" in rule:
            assert record["status"] == rule["status"], name


def test_every_spoken_question_reads_its_prompt_and_every_choice():
    for path in sorted(SCENARIOS.glob("*/survey.*.json")):
        survey = json.loads(path.read_text())
        spoken = run(path.parent.name, survey["language"], [])["lines"]["items"]
        for item in survey["items"]:
            assert spoken[item["item_id"]].startswith(item["prompt"])
            for option in item.get("options", []):
                assert option["label"] in spoken[item["item_id"]], (path, option)


GENERATED = ROOT / "docs/voice/generated"


def test_the_generated_surveys_are_current_valid_mel_agent_output():
    subprocess.run(["python3", str(ROOT / "voice/export_surveys.py"), "--check"], check=True)
    index = json.loads((GENERATED / "index.json").read_text())
    assert index, "the voice demo needs at least one survey the MEL agent wrote"
    for entry in index:
        survey = json.loads((GENERATED / entry["file"]).read_text())
        assert CONTRACTS.errors("survey", survey) == []
        assert survey["language"] == entry["language"]
        assert entry["insight_id"] in survey["source_insight_ids"]
        assert entry["model_id"]


def test_a_generated_survey_plays_to_a_valid_record():
    entry = json.loads((GENERATED / "index.json").read_text())[0]
    survey = json.loads((GENERATED / entry["file"]).read_text())
    steps = [
        {"item_id": item["item_id"], "value": item["options"][0]["value"], "heard": ""}
        for item in survey["items"]
    ]
    payload = json.dumps({"survey": survey, "steps": steps, "finish": {}})
    harness = HARNESS % json.dumps(MODULE.as_uri())
    out = subprocess.run(
        ["node", "--input-type=module", "-e", harness, payload],
        capture_output=True,
        text=True,
        check=True,
    )
    record = json.loads(out.stdout)["record"]
    assert_valid(record)
    assert record["complete"] is True


def test_the_page_and_the_recorder_name_a_clip_the_same_way():
    result = run("1-mustard-aphid-spray", "hi-IN", [])
    text = result["lines"]["opening"]
    expected = hashlib.sha256(f"hi-IN\n{text}".encode()).hexdigest()[:16] + ".mp3"
    assert result["clip"] == expected


def test_every_line_the_page_speaks_has_a_recording():
    subprocess.run([sys.executable, str(ROOT / "voice/render_audio.py"), "--check"], check=True)
    manifest = json.loads((ROOT / "docs/voice/audio/index.json").read_text())
    for name in manifest["clips"]:
        assert (ROOT / "docs/voice/audio" / name).stat().st_size > 1000, name
