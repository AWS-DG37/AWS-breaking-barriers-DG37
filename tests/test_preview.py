"""The local preview uses the real graph while keeping all persistence local."""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from mel.config import Settings, load_config
from mel.models import Review, SurveyDraft
from mel.worker import DEFAULT_ROW_LIMIT, preview


def fake_agents():
    draft = {
        "rationale": "Measure adoption.",
        "items": [
            {
                "item_id": "q1",
                "type": "survey-confirm",
                "prompt": "Did you do it?",
                "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}],
            }
        ],
    }
    review = {
        "relevance_score": 0.95,
        "language_correct": True,
        "gives_new_advice": False,
        "asks_personal_info": False,
        "issues": [],
        "suggestions": [],
    }
    return (
        Agent(TestModel(custom_output_args=draft), output_type=SurveyDraft),
        Agent(TestModel(custom_output_args=review), output_type=Review),
    )


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    def args(self, directory, *, full=False, config=None):
        return argparse.Namespace(
            input=Path("tests/fixtures/smoke_insights.jsonl"),
            output_dir=Path(directory),
            full=full,
            config=config,
        )

    async def test_default_ten_rows_without_database_or_s3(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("mel.worker.build_agents", return_value=fake_agents()),
                patch("mel.worker.Store") as store,
                patch("mel.bundle.boto3.client", side_effect=AssertionError("AWS storage used")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await preview(self.args(directory), Settings()), 0)
            store.assert_not_called()
            run = next(Path(directory).iterdir())
            results = [json.loads(s) for s in (run / "results.jsonl").read_text().splitlines()]
            summary = json.loads((run / "summary.json").read_text())
            manifest = json.loads((run / "run.json").read_text())
            self.assertEqual(len(results), DEFAULT_ROW_LIMIT)
            surveys = {r["survey"]["survey_id"] for r in results if "survey" in r}
            self.assertEqual(len(surveys), 6)
            self.assertEqual(summary["counts"], {"accepted": 6, "skipped": 4})
            self.assertEqual(
                sorted(r["rule"] for r in results if r["status"] == "skipped"),
                ["R2", "R3", "R5", "R7"],
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["mode"], "local_preview")
            self.assertEqual(manifest["selection"], f"first_{DEFAULT_ROW_LIMIT}")
            self.assertTrue((run / "bundle.json").exists())

    async def test_full_is_explicit_and_includes_no_survey_example(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("mel.worker.build_agents", return_value=fake_agents()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await preview(self.args(directory, full=True), Settings()), 0)
            summary = json.loads(next(Path(directory).glob("*/summary.json")).read_text())
            self.assertEqual(summary["counts"], {"accepted": 6, "skipped": 4})

    async def test_prompt_edits_are_loaded_on_every_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "generate.txt"
            config = load_config(Path("config/agent.yaml")).model_dump(mode="json")
            config["prompts"]["generate"] = str(prompt)
            config_path = root / "agent.yaml"
            config_path.write_text(yaml.safe_dump(config))
            for text in ("First prompt version", "Second prompt version"):
                prompt.write_text(text)
                with (
                    patch("mel.worker.build_agents", return_value=fake_agents()) as build,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    args = self.args(root / "runs", config=config_path)
                    self.assertEqual(await preview(args, Settings()), 0)
                    self.assertEqual(build.call_args.args[2]["generate"], text)
            runs = list((root / "runs").iterdir())
            self.assertEqual(len(runs), 2)
            texts = {
                json.loads((run / "bundle.json").read_text())["prompts"]["generate"] for run in runs
            }
            self.assertEqual(texts, {"First prompt version", "Second prompt version"})

    async def test_model_failure_keeps_results_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("mel.worker.build_agents", return_value=(None, None)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(await preview(self.args(directory), Settings()), 1)
            summary = json.loads(next(Path(directory).glob("*/summary.json")).read_text())
            self.assertEqual(summary["counts"], {"failed": 6, "skipped": 4})
            self.assertFalse(summary["usage_complete"])
            self.assertIsNone(summary["input_tokens"])


if __name__ == "__main__":
    unittest.main()
