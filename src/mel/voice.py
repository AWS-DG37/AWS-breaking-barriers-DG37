"""Recorded speech for the voice survey page.

The page (docs/voice/voice-survey.mjs) reads each line of a survey aloud from an MP3 file.
The file name is clip_name(language, text), so a clip exists once per distinct line and a
changed question gets a new clip. When the worker accepts a survey, it records the clips
that are not stored yet. The wording here must be exactly what spokenLines() in
docs/voice/survey-session.mjs says. tests/test_voice_audio.py compares the two in Node.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# "Mahi", a warm Indian female voice from the ElevenLabs library, in a model that speaks
# Hindi and English. The clip name does not include the voice: after a change here,
# record into a new, empty prefix.
VOICE = {
    "voice_id": "OwA6IqdLakQOd19pSLOn",
    "model_id": "eleven_multilingual_v2",
    "voice_settings": {"stability": 0.6, "similarity_boost": 0.8, "speed": 0.92},
}
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_64"
# Where the worker stores clips. The console serves this prefix at /voice/audio/.
S3_PREFIX = "voice/audio/"

# Same text as VOICE_LINES in docs/voice/survey-session.mjs.
VOICE_LINES = {
    "en": {
        "opening": (
            "Hello, this is FarmerChat. I have {n} short questions about the advice you got. "
            "Tap your answer on the screen."
        ),
        "closing": "Thank you. Your answers help us give better advice.",
        "closing_if_follow_up": (
            "Thank you. We have noted this. FarmerChat will contact you again about it."
        ),
        "or": "Or",
        "stop": ".",
        "scale": "Tap a number from {min} to {max}.",
    },
    "hi": {
        "opening": (
            "नमस्ते, यह FarmerChat है। आपको मिली सलाह के बारे में {n} छोटे सवाल हैं। स्क्रीन पर अपना जवाब छूएँ।"
        ),
        "closing": "धन्यवाद। आपके जवाब से हम और अच्छी सलाह दे पाएँगे।",
        "closing_if_follow_up": (
            "धन्यवाद। हमने यह नोट कर लिया है। FarmerChat आपसे इस बारे में फिर बात करेगा।"
        ),
        "or": "या",
        "stop": "।",
        "scale": "{min} से {max} तक कोई नंबर छूएँ।",
    },
}


def voice_language(survey: dict) -> str:
    return "hi" if survey["language"].split("-")[0] == "hi" else "en"


def spoken_question(survey: dict, item: dict) -> str:
    lines = VOICE_LINES[voice_language(survey)]
    if item["type"] == "survey-scale":
        scale = lines["scale"].format(min=item["scale_min"], max=item["scale_max"])
        return f"{item['prompt']} {scale}"
    labels = [option["label"] for option in item["options"]]
    choices = [*labels[:-1], f"{lines['or']} {labels[-1]}"]
    return f"{item['prompt']} " + " ".join(choice + lines["stop"] for choice in choices)


def spoken_lines(survey: dict) -> dict:
    lines = VOICE_LINES[voice_language(survey)]
    return {
        "opening": lines["opening"].format(n=len(survey["items"])),
        "items": {item["item_id"]: spoken_question(survey, item) for item in survey["items"]},
        "closing": lines["closing"],
        "closing_if_follow_up": lines["closing_if_follow_up"],
    }


def clip_name(language: str, text: str) -> str:
    """The first 16 hex digits of SHA-256("<language>\\n<text>"), as clipName() in the page."""
    return hashlib.sha256(f"{language}\n{text}".encode()).hexdigest()[:16] + ".mp3"


def survey_clips(survey: dict) -> dict[str, str]:
    """Clip name to spoken text, for every line the page can say for this survey."""
    lines = spoken_lines(survey)
    texts = [lines["opening"], *lines["items"].values()]
    texts += [lines["closing"], lines["closing_if_follow_up"]]
    return {clip_name(survey["language"], text): text for text in texts}


class VoiceRecordingFailed(RuntimeError):
    """A survey does not have all of its audio. It must not go out on the voice channel."""


class ClipStore(Protocol):
    def has(self, name: str) -> bool: ...
    def put(self, name: str, audio: bytes) -> None: ...


class S3Clips:
    def __init__(self, bucket: str, client) -> None:
        self.bucket, self.client = bucket, client

    def has(self, name: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=S3_PREFIX + name)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def put(self, name: str, audio: bytes) -> None:
        # A clip name is a hash of its text, so a stored clip never changes.
        self.client.put_object(
            Bucket=self.bucket,
            Key=S3_PREFIX + name,
            Body=audio,
            ContentType="audio/mpeg",
            CacheControl="public, max-age=31536000, immutable",
        )


class ElevenLabs:
    """Text to speech. `key` is called at most once, on the first recording."""

    def __init__(self, key: Callable[[], str], timeout: float = 60) -> None:
        self._key_source, self._key, self.timeout = key, None, timeout

    def speak(self, text: str) -> bytes:
        if self._key is None:
            self._key = self._key_source().strip()
        body = {
            "text": text,
            "model_id": VOICE["model_id"],
            "voice_settings": VOICE["voice_settings"],
        }
        request = urllib.request.Request(
            TTS_URL.format(voice_id=VOICE["voice_id"]),
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "xi-api-key": self._key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise RuntimeError(f"ElevenLabs returned HTTP {error.code}: {detail}") from None


@dataclass
class VoiceRecorder:
    """Records the clips of a survey that the store does not have yet."""

    clips: ClipStore
    speak: Callable[[str], bytes]
    attempts: int = 3
    wait: Callable[[float], None] = time.sleep

    def record(self, survey: dict) -> dict:
        needed = survey_clips(survey)
        recorded = []
        for name, text in needed.items():
            if self._attempt(name, lambda: self.clips.has(name)):
                continue
            audio = self._attempt(name, lambda: self.speak(text))
            if not audio:
                raise VoiceRecordingFailed(f"{name}: text to speech returned no audio")
            self._attempt(name, lambda: self.clips.put(name, audio))
            recorded.append(name)
        return {"clips": list(needed), "recorded": recorded}

    def _attempt(self, name: str, step: Callable):
        # ponytail: fixed exponential backoff (1 s, 2 s). It also retries errors that a
        # retry cannot fix, such as an expired key; at most `attempts` calls per survey.
        for attempt in range(1, self.attempts + 1):
            try:
                return step()
            except Exception as error:
                if attempt == self.attempts:
                    raise VoiceRecordingFailed(
                        f"{name}: {type(error).__name__}: {error} (after {attempt} attempts)"
                    ) from error
                self.wait(2 ** (attempt - 1))


@dataclass
class VoiceOff:
    """Used when recording is not configured: every survey lacks audio."""

    reason: str

    def record(self, survey: dict) -> dict:
        raise VoiceRecordingFailed(self.reason)


def recorder(settings) -> VoiceRecorder | VoiceOff:
    """The worker's recorder: clips in the data bucket, the key from Secrets Manager."""
    if not (settings.voice_bucket and settings.voice_key_secret_arn):
        return VoiceOff(
            "Voice recording is not configured. Set MEL_VOICE_BUCKET and MEL_VOICE_KEY_SECRET_ARN."
        )
    import boto3

    secrets = boto3.client("secretsmanager", region_name=settings.aws_region)
    tts = ElevenLabs(
        lambda: secrets.get_secret_value(SecretId=settings.voice_key_secret_arn)["SecretString"]
    )
    s3 = boto3.client("s3", region_name=settings.aws_region)
    return VoiceRecorder(S3Clips(settings.voice_bucket, s3), tts.speak)
