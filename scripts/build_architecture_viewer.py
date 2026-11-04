"""Build the standalone architecture walkthrough from shared player sources."""

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(destination):
    destination.mkdir(parents=True, exist_ok=True)
    assets = destination / "explorer-preview"
    assets.mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "docs/architecture-walkthrough.html", destination / "index.html")
    source = ROOT / "src/mel/explorer/static"
    for name in (
        "survey-player.mjs", "survey-player.css", "dom.mjs", "recorded-preview.mjs",
        "demo-language.mjs",
    ):
        shutil.copyfile(source / name, assets / name)
    shutil.copyfile(source / "assets/mustard-field.webp", assets / "mustard-field.webp")
    recorded = json.loads((ROOT / "src/mel/explorer/demo/recorded-run.json").read_text())
    catalog = []
    for run in recorded["runs"]:
        outcome = run["results"][0]["outcome"]
        catalog.append(
            {
                "label": run["manifest"]["label"],
                "insight_id": run["results"][0]["insight_id"],
                "status": outcome["status"],
                "reason": outcome.get("reason"),
                "survey": outcome.get("survey"),
                "candidate_survey": outcome.get("candidate_survey"),
                "question_evidence": outcome.get("question_evidence", []),
                "error_type": next(
                    (
                        event["data"]["error_type"]
                        for event in outcome.get("events", [])
                        if "error_type" in event["data"]
                    ),
                    None,
                ),
            }
        )
    (assets / "surveys.json").write_text(
        json.dumps(
            {"candidates": catalog, "provenance": recorded["provenance"]}, ensure_ascii=False
        )
    )
    print(f"Built architecture viewer at {destination.resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    build(args.destination)


if __name__ == "__main__":
    main()
