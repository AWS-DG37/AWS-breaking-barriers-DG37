"""Record every line the repository's voice demo speaks, once, with ElevenLabs.

This fills docs/voice/audio for the GitHub Pages copy of the page, which plays only these
MP3 files. The hosted worker records the surveys it accepts by itself (src/mel/voice.py),
with the same wording, voice and clip names.

    ELEVENLABS_API_KEY=... uv run python voice/render_audio.py   # record missing clips
    uv run python voice/render_audio.py --check                   # fail if a clip is missing

Run it after voice/export_surveys.py adds surveys. Changing VOICE re-records every clip.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from mel.voice import VOICE, ElevenLabs, survey_clips

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/voice/audio"
MANIFEST = OUT / "index.json"
GENERATED = ROOT / "docs/voice/generated"
SCENARIOS = ROOT / "docs/participant-pack/json/scenarios"


def surveys() -> list[dict]:
    paths = [GENERATED / e["file"] for e in json.loads((GENERATED / "index.json").read_text())]
    paths += sorted(SCENARIOS.glob("*/survey.*.json"))
    return [json.loads(p.read_text()) for p in paths]


def needed_clips() -> dict[str, dict]:
    clips = {}
    for survey in surveys():
        for name, text in survey_clips(survey).items():
            clips[name] = {"language": survey["language"], "text": text}
    return dict(sorted(clips.items()))


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
    tts = ElevenLabs(lambda: key, timeout=120)
    for name in missing:
        try:
            (OUT / name).write_bytes(tts.speak(clips[name]["text"]))
        except RuntimeError as error:
            sys.exit(f"Text to speech failed: {error}")
        print(f"Recorded {name}: {clips[name]['text'][:70]}")
    for stale in set(p.name for p in OUT.glob("*.mp3")) - clips.keys():
        (OUT / stale).unlink()
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(clips)} clips current, {len(missing)} recorded now")


if __name__ == "__main__":
    main()
