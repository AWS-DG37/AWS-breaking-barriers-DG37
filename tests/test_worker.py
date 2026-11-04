"""Exercise batch checkpoint/resume and exports without AWS or PostgreSQL."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from memory_configs import MemoryConfigs
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from mel.config import Settings
from mel.datasets import parse_insights
from mel.models import Review, SurveyDraft
from mel.worker import DEFAULT_ROW_LIMIT, run_batch


class MemoryStore(MemoryConfigs):
    def __init__(self):
        super().__init__()
        self.records = {}
        self.number = 0
        self.saved = 0
        self.status = None

    def initialize(self):
        pass

    def start_run(self, run_id, fingerprint, manifest, **binding):
        self.binding = binding

    def completed(self, run_id, retry_failed):
        return {
            key
            for key, value in self.records.items()
            if not retry_failed or value["status"] != "failed"
        }

    def next_survey_id(self):
        self.number += 1
        return f"S-{self.number:010d}"

    def save(self, run_id, insight, outcome):
        self.saved += 1
        self.records[outcome.insight_id] = outcome.model_dump(mode="json", exclude_none=True)

    def results(self, run_id):
        return list(self.records.values())

    def finish(self, run_id, status):
        self.status = status

    def close(self):
        pass


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def run_smoke(self, full=False, source="tests/fixtures/smoke_insights.jsonl"):
        store = MemoryStore()
        draft = {
            "rationale": "Check adoption of the original advice.",
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
        }
        agents = (
            Agent(TestModel(custom_output_args=draft), output_type=SurveyDraft),
            Agent(TestModel(custom_output_args=review), output_type=Review),
        )
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                input=source,
                output=str(Path(directory) / "results.jsonl"),
                run_id=str(uuid4()),
                bundle_uri=None,
                retry_failed=False,
                full=full,
            )
            with (
                patch("mel.worker.Store", return_value=store),
                patch("mel.worker.build_agents", return_value=agents),
            ):
                settings = Settings(bedrock_model_id="test-model")
                self.assertEqual(await run_batch(args, settings), 0)
                first_saved = store.saved
                self.assertEqual(await run_batch(args, settings), 0)
                self.assertEqual(store.saved, first_saved)
                exported = [json.loads(line) for line in Path(args.output).read_text().splitlines()]
                self.assertEqual(len(exported), first_saved)
        return store

    async def test_smoke_scenarios_run_offline(self):
        store = await self.run_smoke(full=True)
        self.assertEqual(store.saved, 10)
        self.assertEqual(sum(r["status"] == "accepted" for r in store.records.values()), 6)
        self.assertEqual(sum(r["status"] == "skipped" for r in store.records.values()), 4)

    async def test_default_and_resume_never_process_more_than_the_row_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.jsonl"
            rows = [{"insight_id": f"junk-{n:02d}", "quality_flag": "junk"} for n in range(12)]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            store = await self.run_smoke(source=str(source))
        self.assertEqual(store.saved, DEFAULT_ROW_LIMIT)
        self.assertEqual(
            set(store.records), {row["insight_id"] for row in rows[:DEFAULT_ROW_LIMIT]}
        )

    def test_limit_counts_nonblank_rows_and_does_not_parse_the_tail(self):
        rows = parse_insights(b'\n{"insight_id":"one"}\n\n{"insight_id":"two"}\nbad json', 2)
        self.assertEqual([r["insight_id"] for r in rows], ["one", "two"])

    def test_zero_limit_is_rejected_instead_of_running_all_rows(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            parse_insights(b'{"insight_id":"one"}', 0)

    async def test_resumed_run_exports_previous_checkpoints_without_regeneration(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.jsonl"
            output = Path(directory) / "output.jsonl"
            source.write_text(json.dumps({"insight_id": "junk-1", "quality_flag": "junk"}))
            args = argparse.Namespace(
                input=str(source),
                output=str(output),
                run_id=str(uuid4()),
                bundle_uri=None,
                retry_failed=False,
            )
            agents = (
                Agent(TestModel(), output_type=SurveyDraft),
                Agent(TestModel(), output_type=Review),
            )
            with (
                patch("mel.worker.Store", return_value=store),
                patch("mel.worker.build_agents", return_value=agents),
            ):
                settings = Settings(bedrock_model_id="test-model")
                self.assertEqual(await run_batch(args, settings), 0)
                first_export = output.read_text()
                self.assertEqual(await run_batch(args, settings), 0)
                self.assertEqual(store.saved, 1)
                self.assertEqual(output.read_text(), first_export)
                self.assertEqual(store.status, "completed")

    async def test_provider_failure_is_checkpointed_and_batch_returns_nonzero(self):
        store = MemoryStore()
        pack = Path("docs/participant-pack/json/scenarios/1-mustard-aphid-spray/insight.json")
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                input=str(pack),
                output=str(Path(directory) / "output.jsonl"),
                run_id=str(uuid4()),
                bundle_uri=None,
                retry_failed=False,
            )
            source = Path(directory) / "input.jsonl"
            source.write_text(json.dumps(json.loads(pack.read_text())))
            args.input = str(source)
            with (
                patch("mel.worker.Store", return_value=store),
                patch("mel.worker.build_agents", return_value=(None, None)),
            ):
                # Inject an agent failure at the real graph boundary, not a fabricated outcome.
                self.assertEqual(await run_batch(args, Settings(bedrock_model_id="test-model")), 1)
            self.assertEqual(store.status, "completed_with_errors")
            self.assertEqual(next(iter(store.records.values()))["status"], "failed")


if __name__ == "__main__":
    unittest.main()


class ModelClientCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_closes_distinct_native_clients_once_and_awaits_async_clients(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock

        from mel.worker import close_model_clients

        first, second = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=Mock())
        agents = [
            SimpleNamespace(model=SimpleNamespace(client=client))
            for client in (first, first, second)
        ]
        await close_model_clients(*agents)
        first.close.assert_awaited_once()
        second.close.assert_called_once()
