"""Copy the surveys the MEL agent accepted in a recorded run into docs/voice/generated/.

The voice page lists these first, so the demo plays real agent output, not the
participant pack's reference surveys. GitHub Pages serves only docs/, so the files are
copied there with their provenance (model, config version, run, insight, advice).

    uv run python voice/export_surveys.py                  # from the bundled recorded run
    uv run python voice/export_surveys.py --run other.json  # from another explorer export
    uv run python voice/export_surveys.py --check           # fail if the files are stale

ponytail: reads the explorer's recorded-run format only. Export from the experiment
store or the worker's results JSONL when a demo needs more than the recorded run.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "src/mel/explorer/demo/recorded-run.json"
OUT = ROOT / "docs/voice/generated"
DEMO_LANGUAGE = ROOT / "src/mel/explorer/static/demo-language.mjs"


def dump(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def exports(run_paths: list[Path]) -> dict[str, str]:
    """File name -> file text, for every accepted survey plus index.json."""
    files, index = {}, []
    for path in run_paths:
        recorded = json.loads(path.read_text())
        provenance = recorded.get("provenance", {})
        for run in recorded["runs"]:
            manifest = run["manifest"]
            generation = manifest["config"]["generation"]
            for result in run["results"]:
                outcome = result["outcome"]
                survey = outcome.get("survey")
                if outcome.get("status") != "accepted" or not survey:
                    continue
                name = (
                    f"{slug(manifest['label'])}-{result['insight_id']}-{survey['survey_id']}.json"
                )
                files[name] = dump(survey)
                asset = result["input"]["asset"]["name"]
                index.append(
                    {
                        "file": name,
                        "label": f"{manifest['label']}: {asset}, {survey['language']}",
                        "model_id": generation["model_id"],
                        "config_version": manifest["config"].get("version"),
                        "experiment_id": provenance.get("experiment_id"),
                        "recorded_at": provenance.get("recorded_at"),
                        "insight_id": result["insight_id"],
                        "advice_summary": result["input"]["advice_summary"],
                        "language": survey["language"],
                    }
                )
    files["index.json"] = dump(index)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run", type=Path, action="append", help="recorded run JSON (repeatable)")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if docs/voice/generated is stale"
    )
    args = parser.parse_args()

    files = exports(args.run or [DEFAULT_RUN])
    current = {p.name: p.read_text() for p in OUT.glob("*.json")} if OUT.exists() else {}
    if args.check:
        if current != files:
            sys.exit("docs/voice/generated is stale. Run: uv run python voice/export_surveys.py")
        shared = OUT / DEMO_LANGUAGE.name
        if not shared.exists() or shared.read_bytes() != DEMO_LANGUAGE.read_bytes():
            sys.exit("Demo translations are stale. Run: uv run python voice/export_surveys.py")
        print(f"docs/voice/generated is current ({len(files) - 1} surveys)")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    for name in current.keys() - files.keys():
        (OUT / name).unlink()
    for name, text in files.items():
        (OUT / name).write_text(text)
    (OUT / DEMO_LANGUAGE.name).write_bytes(DEMO_LANGUAGE.read_bytes())
    print(f"Wrote {len(files) - 1} surveys and index.json to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
