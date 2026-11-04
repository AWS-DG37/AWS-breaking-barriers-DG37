"""Record every line the voice page speaks, once, with ElevenLabs text to speech.

The page only plays these MP3 files, so it needs no API key, no microphone and no live
service. A clip is named after the first 16 hex digits of SHA-256("<language>\\n<text>"),
the same name clipName() in docs/voice/survey-session.mjs computes, so a changed question
gets a new clip and an unchanged one is never recorded twice.

    ELEVENLABS_API_KEY=... uv run python voice/render_audio.py   # record missing clips
    uv run python voice/render_audio.py --check                   # fail if a clip is missing

Run it after voice/export_surveys.py adds surveys. Changing VOICE re-records every clip.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/voice/audio"
MANIFEST = OUT / "index.json"
MODULE = ROOT / "docs/voice/survey-session.mjs"
GENERATED = ROOT / "docs/voice/generated"
SCENARIOS = ROOT / "docs/participant-pack/json/scenarios"
# "Mahi", a warm Indian female voice from the ElevenLabs library, in a model that speaks
# Hindi and English.
VOICE = {
    "voice_id": "OwA6IqdLakQOd19pSLOn",
    "model_id": "eleven_multilingual_v2",
    "voice_settings": {"stability": 0.6, "similarity_boost": 0.8, "speed": 0.92},
}
TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_64"


def clip_name(language: str, text: str) -> str:
    return hashlib.sha256(f"{language}\n{text}".encode()).hexdigest()[:16] + ".mp3"


def surveys() -> list[dict]:
    paths = [GENERATED / e["file"] for e in json.loads((GENERATED / "index.json").read_text())]
    paths += sorted(SCENARIOS.glob("*/survey.*.json"))
    return [json.loads(p.read_text()) for p in paths]


def spoken_lines(survey: dict) -> dict:
    script = (
        f"import {{spokenLines}} from {json.dumps(MODULE.as_uri())};"
        "console.log(JSON.stringify(spokenLines(JSON.parse(process.argv[1]))));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(survey)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def needed_clips() -> dict[str, dict]:
    clips = {}
    for survey in surveys():
        lines = spoken_lines(survey)
        texts = [lines["opening"], *lines["items"].values()]
        texts += [lines["closing"], lines["closing_if_follow_up"]]
        for text in texts:
            clips[clip_name(survey["language"], text)] = {
                "language": survey["language"],
                "text": text,
            }
    return dict(sorted(clips.items()))


def record(text: str, key: str) -> bytes:
    body = {"text": text, "model_id": VOICE["model_id"], "voice_settings": VOICE["voice_settings"]}
    req = urllib.request.Request(
        TTS.format(voice_id=VOICE["voice_id"]),
        data=json.dumps(body).encode(),
        method="POST",
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        sys.exit(f"Text to speech failed with HTTP {err.code}: {err.read().decode()[:1000]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if a clip is missing or stale")
    args = parser.parse_args()

    clips = needed_clips()
    manifest = {"voice_id": VOICE["voice_id"], "model_id": VOICE["model_id"], "clips": clips}
    old = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    same_voice = (old.get("voice_id"), old.get("model_id")) == (
        VOICE["voice_id"],
        VOICE["model_id"],
    )
    missing = [n for n in clips if not same_voice or not (OUT / n).exists()]
    if args.check:
        if missing or old != manifest:
            sys.exit(
                f"{len(missing)} clips missing or stale. Run: uv run python voice/render_audio.py"
            )
        print(f"docs/voice/audio is current ({len(clips)} clips)")
        return
    key = os.environ.get("ELEVENLABS_API_KEY")
    if missing and not key:
        sys.exit("Set ELEVENLABS_API_KEY. The key is never stored in this repository.")
    OUT.mkdir(parents=True, exist_ok=True)
    for name in missing:
        (OUT / name).write_bytes(record(clips[name]["text"], key))
        print(f"Recorded {name}: {clips[name]['text'][:70]}")
    for stale in set(p.name for p in OUT.glob("*.mp3")) - clips.keys():
        (OUT / stale).unlink()
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(clips)} clips current, {len(missing)} recorded now")


if __name__ == "__main__":
    main()
