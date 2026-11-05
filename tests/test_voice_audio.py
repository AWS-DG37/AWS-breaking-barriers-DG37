"""The worker records the speech of each accepted survey, once per distinct line.

The page speaks with docs/voice/survey-session.mjs and the worker records with
src/mel/voice.py. Both must produce the same text and the same clip names, or the page
asks for a clip the worker never recorded.
"""

import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg
import pytest
from botocore.exceptions import ClientError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from mel import voice as voice_module
from mel.bundle import Bundle
from mel.clock import IST
from mel.config import Settings
from mel.graph import Dependencies
from mel.handoff import without_channel
from mel.models import Review, SurveyDraft
from mel.voice import (
    S3_PREFIX,
    ElevenLabs,
    S3Clips,
    VoiceOff,
    VoiceRecorder,
    VoiceRecordingFailed,
    louder,
    recorder,
    spoken_lines,
    survey_clips,
)
from mel.worker import process_insight

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "docs/voice/survey-session.mjs"
SCENARIOS = ROOT / "docs/participant-pack/json/scenarios"
GENERATED = ROOT / "docs/voice/generated"


def reference_surveys() -> list[dict]:
    paths = sorted(SCENARIOS.glob("*/survey.*.json"))
    paths += [GENERATED / e["file"] for e in json.loads((GENERATED / "index.json").read_text())]
    return [json.loads(path.read_text()) for path in paths]


SURVEY = json.loads((SCENARIOS / "1-mustard-aphid-spray/survey.en-IN.json").read_text())


class Clips:
    def __init__(self, names=()):
        self.stored = {name: b"x" * 2000 for name in names}

    def has(self, name):
        return name in self.stored

    def put(self, name, audio):
        self.stored[name] = audio


class Speaker:
    def __init__(self, fail=0):
        self.texts, self.fail = [], fail

    def __call__(self, text):
        self.texts.append(text)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("ElevenLabs returned HTTP 503")
        return b"\xff\xf3" * 1000


def voice(clips=None, speaker=None, attempts=3):
    return VoiceRecorder(clips or Clips(), speaker or Speaker(), attempts, wait=lambda _: None)


# ---- Python and the page say the same thing ---------------------------------------------

JS = """
import {spokenLines, clipName} from %s;
const out = [];
for (const survey of JSON.parse(process.argv[1])) {
  const lines = spokenLines(survey);
  const texts = [lines.opening, ...Object.values(lines.items),
    lines.closing, lines.closing_if_follow_up];
  const names = [];
  for (const text of texts) names.push(await clipName(survey.language, text));
  out.push({lines, names});
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_python_speaks_and_names_clips_exactly_as_the_page_does():
    surveys = reference_surveys()
    scale = next(s for s in surveys if s["language"] == "hi-IN" and "scale" in json.dumps(s))
    assert scale, "the comparison must cover a Hindi scale question"
    out = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            JS % json.dumps(MODULE.as_uri()),
            json.dumps(surveys),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for survey, page in zip(surveys, json.loads(out.stdout), strict=True):
        assert spoken_lines(survey) == page["lines"], survey["survey_id"]
        assert list(survey_clips(survey)) == page["names"], survey["survey_id"]


# ---- Only missing clips are recorded ----------------------------------------------------


def test_a_survey_whose_clips_are_stored_makes_no_text_to_speech_call():
    speaker = Speaker()
    result = voice(Clips(survey_clips(SURVEY)), speaker).record(SURVEY)
    assert speaker.texts == []
    assert result == {"clips": list(survey_clips(SURVEY)), "recorded": []}


def test_a_changed_question_records_one_new_clip():
    clips = Clips()
    voice(clips).record(SURVEY)
    changed = json.loads(json.dumps(SURVEY))
    changed["items"][1]["prompt"] = "Was it easy to buy the spray?"
    speaker = Speaker()
    result = voice(clips, speaker).record(changed)
    assert len(speaker.texts) == 1
    assert speaker.texts[0].startswith("Was it easy to buy the spray?")
    assert len(result["recorded"]) == 1
    assert set(clips.stored) >= set(survey_clips(changed))


def test_a_failed_call_is_retried():
    speaker = Speaker(fail=2)
    clips = Clips()
    voice(clips, speaker).record(SURVEY)
    assert set(clips.stored) == set(survey_clips(SURVEY))


def test_a_line_that_keeps_failing_raises_and_stores_nothing_for_it():
    speaker = Speaker(fail=99)
    clips = Clips()
    with pytest.raises(VoiceRecordingFailed, match="after 3 attempts"):
        voice(clips, speaker).record(SURVEY)
    assert len(speaker.texts) == 3
    assert clips.stored == {}


def test_recording_stops_when_its_time_budget_is_spent():
    now = [0.0]

    def slow(text):
        now[0] += 50
        raise RuntimeError("ElevenLabs timed out")

    recorder_ = VoiceRecorder(Clips(), slow, wait=lambda s: None, budget_seconds=90)
    recorder_.clock = lambda: now[0]
    with pytest.raises(VoiceRecordingFailed, match="no time left"):
        recorder_.record(SURVEY)
    assert now[0] == 100, "the third attempt would start after the budget, so it does not"


def test_empty_audio_is_a_failure():
    with pytest.raises(VoiceRecordingFailed, match="no audio"):
        voice(speaker=lambda text: b"").record(SURVEY)


# ---- Loudness ----------------------------------------------------------------------------

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def quiet_mp3(seconds=5) -> bytes:
    """A tone about as quiet as ElevenLabs speech (-24 LUFS) would be, at -36 LUFS."""
    return subprocess.run(
        [FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i",
         f"sine=frequency=220:duration={seconds},volume=0.02",
         "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip


def loudness(mp3: bytes) -> tuple[float, float]:
    """Integrated loudness (LUFS) and true peak (dBTP), as ffmpeg's EBU R128 meter reads them."""
    log = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-f", "mp3", "-i", "pipe:0",
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        input=mp3, capture_output=True, check=True,
    ).stderr.decode()  # fmt: skip
    summary = log[log.rindex("Summary:") :].split()
    return float(summary[summary.index("I:") + 1]), float(summary[summary.index("Peak:") + 1])


def test_a_quiet_recording_comes_out_at_speech_loudness():
    assert loudness(quiet_mp3())[0] < -30
    integrated, peak = loudness(louder(quiet_mp3()))
    assert -17 <= integrated <= -15
    assert peak <= -1.0


def test_audio_that_does_not_decode_is_an_error():
    with pytest.raises(RuntimeError, match="ffmpeg"):
        louder(b"not audio")


def test_text_to_speech_returns_the_normalized_clip(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return quiet_mp3()

    monkeypatch.setattr(voice_module.urllib.request, "urlopen", lambda *a, **k: Response())
    assert -17 <= loudness(ElevenLabs(lambda: "key").speak("Hello"))[0] <= -15


def test_every_committed_demo_clip_is_at_speech_loudness():
    clips = sorted((ROOT / "docs/voice/audio").glob("*.mp3"))
    assert clips
    quiet = {}
    for clip in clips:
        integrated, peak = loudness(clip.read_bytes())
        if not (-17 <= integrated <= -15 and peak <= -1.0):
            quiet[clip.name] = (integrated, peak)
    assert not quiet, f"Not at -16 LUFS: {quiet}"


def test_without_configuration_every_survey_lacks_audio():
    off = recorder(Settings())
    assert isinstance(off, VoiceOff)
    with pytest.raises(VoiceRecordingFailed, match="MEL_VOICE_BUCKET"):
        off.record(SURVEY)


# ---- S3 storage --------------------------------------------------------------------------


class S3:
    def __init__(self, code=None):
        self.code, self.puts = code, []

    def head_object(self, Bucket, Key):
        if self.code:
            raise ClientError({"Error": {"Code": self.code}}, "HeadObject")

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


def test_s3_store_reads_a_missing_clip_as_missing_and_other_errors_as_errors():
    assert S3Clips("bucket", S3()).has("a.mp3") is True
    assert S3Clips("bucket", S3("404")).has("a.mp3") is False
    with pytest.raises(ClientError):
        S3Clips("bucket", S3("403")).has("a.mp3")


def test_s3_store_writes_under_the_voice_audio_prefix():
    s3 = S3()
    S3Clips("bucket", s3).put("a.mp3", b"mp3")
    (put,) = s3.puts
    assert (put["Bucket"], put["Key"], put["ContentType"]) == (
        "bucket",
        S3_PREFIX + "a.mp3",
        "audio/mpeg",
    )


# ---- The graph: no audio, no voice channel -------------------------------------------


def channels(handoff):
    return [entry["channel"] for entry in handoff["channel_order"]]


def test_removing_a_channel_keeps_the_last_wait_open_ended():
    handoff = {
        "channel_order": [
            {"channel": "app", "wait_hours": 24},
            {"channel": "whatsapp", "wait_hours": 24},
            {"channel": "voice", "wait_hours": None},
        ]
    }
    assert without_channel(handoff, "voice")["channel_order"] == [
        {"channel": "app", "wait_hours": 24},
        {"channel": "whatsapp", "wait_hours": None},
    ]
    assert channels(handoff) == ["app", "whatsapp", "voice"], "the input is not changed"


class Fixed(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 12, 4, 23, tzinfo=IST)


def accept(voice_recorder, monkeypatch, timeout_seconds=None):
    """Accept the mustard survey (as tests/test_graph.py drafts it) for a farmer who
    prefers the voice channel."""
    bundle = Bundle.load(Settings(bedrock_model_id="test-model"))
    insight = json.loads((SCENARIOS / "1-mustard-aphid-spray/insight.json").read_text())
    insight["preferred_channel"] = "voice"
    survey = json.loads((SCENARIOS / "1-mustard-aphid-spray/survey.hi-IN.json").read_text())
    items = [survey["items"][0], survey["items"][2]]
    items[1]["options"] = [
        {"value": "better", "label": "पहले से कम"},
        {"value": "same", "label": "पहले जितने"},
        {"value": "worse", "label": "पहले से ज़्यादा", "follow_up": "crop_or_animal_damaged"},
    ]
    draft = {"items": items, "rationale": survey["rationale"]}
    review = {
        "relevance_score": 0.95,
        "language_correct": True,
        "gives_new_advice": False,
        "asks_personal_info": False,
        "issues": [],
    }
    deps = Dependencies.from_bundle(
        bundle,
        Agent(TestModel(custom_output_args=draft), output_type=SurveyDraft),
        Agent(TestModel(custom_output_args=review), output_type=Review),
        voice=voice_recorder,
    )
    if timeout_seconds is not None:
        deps.config.graph.insight_timeout_seconds = timeout_seconds
    monkeypatch.setattr("mel.worker.datetime", Fixed)
    outcome = asyncio.run(process_insight(insight, "S-0000000001", deps))
    assert outcome.status == "accepted", outcome.reason
    bundle.contracts.validate("schedule_handoff", outcome.handoff)
    events = [e.data for e in outcome.events if e.stage == "voice"]
    return outcome, events


def test_an_accepted_survey_is_recorded_and_keeps_the_voice_channel(monkeypatch):
    clips = Clips()
    outcome, (event,) = accept(voice(clips), monkeypatch)
    assert channels(outcome.handoff)[0] == "voice"
    assert set(clips.stored) == set(survey_clips(outcome.survey))
    assert event["status"] == "recorded"
    assert event["clips"] == list(survey_clips(outcome.survey))


def test_a_survey_without_its_audio_is_not_scheduled_for_voice(caplog, monkeypatch):
    outcome, (event,) = accept(voice(speaker=Speaker(fail=99)), monkeypatch)
    assert "voice" not in channels(outcome.handoff)
    assert outcome.handoff["channel_order"][-1]["wait_hours"] is None
    assert event["status"] == "failed"
    assert "after 3 attempts" in event["error"]
    assert "voice_recording_failed" in caplog.text


def test_slow_recording_does_not_count_toward_the_survey_time_limit(monkeypatch):
    def slow(text):
        time.sleep(0.25)
        return b"\xff\xf3" * 1000

    outcome, (event,) = accept(voice(speaker=slow), monkeypatch, timeout_seconds=1)
    assert event["status"] == "recorded"
    assert channels(outcome.handoff)[0] == "voice"


def test_unconfigured_recording_is_not_scheduled_for_voice(monkeypatch):
    outcome, (event,) = accept(recorder(Settings()), monkeypatch)
    assert "voice" not in channels(outcome.handoff)
    assert "MEL_VOICE_BUCKET" in event["error"]


def test_without_a_recorder_the_worker_does_not_touch_the_voice_channel(monkeypatch):
    """Previews and comparisons never schedule a survey, so they record nothing."""
    outcome, events = accept(None, monkeypatch)
    assert channels(outcome.handoff)[0] == "voice"
    assert events == []
