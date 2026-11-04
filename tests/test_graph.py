"""Offline contract/graph tests use real Pydantic AI agents with TestModel, never Bedrock."""

import json
import unittest
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from mel.bundle import Bundle
from mel.clock import IST
from mel.config import Settings
from mel.datasets import parse_insights
from mel.graph import Dependencies, Select, State, survey_graph
from mel.models import Review, SurveyDraft

PACK = Path("docs/participant-pack")


class GraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bundle = Bundle.load(Settings(bedrock_model_id="test-model"))
        self.insight = json.loads(
            (PACK / "json/scenarios/1-mustard-aphid-spray/insight.json").read_text()
        )
        survey = json.loads(
            (PACK / "json/scenarios/1-mustard-aphid-spray/survey.hi-IN.json").read_text()
        )
        draft = {"items": survey["items"], "rationale": survey["rationale"]}
        self.review = {
            "relevance_score": 0.95,
            "language_correct": True,
            "gives_new_advice": False,
            "asks_personal_info": False,
            "issues": [],
        }
        self.deps = Dependencies.from_bundle(
            self.bundle,
            Agent(TestModel(custom_output_args=draft), output_type=SurveyDraft),
            Agent(TestModel(custom_output_args=self.review), output_type=Review),
        )

    async def execute(self):
        state = State(self.insight, "S-0000000001", datetime(2026, 12, 4, 23, tzinfo=IST))
        return (await survey_graph.run(Select(), state=state, deps=self.deps)).output

    async def test_valid_survey_produces_schema_valid_handoff(self):
        result = await self.execute()
        self.assertEqual(result.status, "accepted")
        self.bundle.contracts.validate("schedule_handoff", result.handoff)
        self.assertEqual(result.survey["source_insight_ids"], [self.insight["insight_id"]])
        self.assertEqual(result.handoff["channel_order"][-1]["wait_hours"], None)
        self.assertGreater(result.input_tokens, 0)

    async def test_low_quality_hits_revision_limit_and_never_emits_handoff(self):
        bad = {**self.review, "relevance_score": 0.1, "issues": ["Not grounded"]}
        self.deps.reviewer = Agent(TestModel(custom_output_args=bad), output_type=Review)
        result = await self.execute()
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.attempts, 2)
        self.assertIsNone(result.handoff)
        self.assertIsNotNone(result.candidate_survey)
        self.assertIsNone(result.survey)

    async def test_optional_suggestions_do_not_veto_a_usable_survey(self):
        review = {
            **self.review,
            "relevance_score": 0.82,
            "suggestions": ["Consider a separate crop-damage question if space permits."],
        }
        self.deps.reviewer = Agent(TestModel(custom_output_args=review), output_type=Review)
        result = await self.execute()
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.attempts, 1)
        self.assertTrue(result.review.suggestions)
        self.assertIsNotNone(result.handoff)

    async def test_blocking_defects_still_reject_high_relevance_surveys(self):
        review = {**self.review, "issues": ["Adoption options overlap and cannot be interpreted."]}
        self.deps.reviewer = Agent(TestModel(custom_output_args=review), output_type=Review)
        result = await self.execute()
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.attempts, 2)
        self.assertIsNone(result.handoff)

    async def test_wrong_language_rejected_even_with_high_relevance(self):
        bad = {**self.review, "language_correct": False}
        self.deps.reviewer = Agent(TestModel(custom_output_args=bad), output_type=Review)
        self.assertEqual((await self.execute()).status, "rejected")

    async def test_personal_information_rejected(self):
        bad = {**self.review, "asks_personal_info": True}
        self.deps.reviewer = Agent(TestModel(custom_output_args=bad), output_type=Review)
        self.assertEqual((await self.execute()).status, "rejected")

    async def test_junk_never_calls_model(self):
        self.insight = {"insight_id": "broken", "quality_flag": "junk"}
        result = await self.execute()
        self.assertEqual((result.rule, result.reason), ("R1", "junk_insight"))
        self.assertEqual(result.attempts, 0)

    async def test_information_returns_pack_compatible_no_survey(self):
        self.insight["resolution_type"] = "generic_information"
        result = await self.execute()
        self.assertEqual((result.status, result.rule), ("skipped", "R3"))
        self.bundle.contracts.validate("no_survey", result.no_survey)

    async def test_critical_severity_goes_to_a_person_without_model_call(self):
        self.insight["severity"] = "critical"
        result = await self.execute()
        self.assertEqual((result.rule, result.reason), ("R2", "critical_needs_human_call"))
        self.assertIsNone(result.no_survey)
        self.assertEqual(result.attempts, 0)

    async def test_invalid_input_is_rejected_before_inference(self):
        del self.insight["farmer_id"]
        result = await self.execute()
        self.assertEqual((result.rule, result.reason), ("R1", "junk_insight"))
        self.assertIn("farmer_id", result.detail)
        self.assertEqual(result.attempts, 0)

    async def test_expired_season_never_calls_model(self):
        self.insight["created_at"] = "2027-03-31T09:12:00+05:30"
        self.assertEqual((await self.execute()).reason, "window_after_season")

    async def test_inconsistent_verification_skips_without_calling_model(self):
        # needed=true but timing=none: contradictory metadata. The graph must route
        # this to a terminal skip at R5b, never reaching the generator or reviewer.
        self.insight["verification"] = {
            **self.insight["verification"],
            "needed": True,
            "timing": "none",
        }
        result = await self.execute()
        self.assertEqual((result.status, result.rule), ("skipped", "R5b"))
        self.assertEqual(result.reason, "invalid_verification_timing")
        self.assertEqual(result.attempts, 0)
        self.assertIsNone(result.no_survey)

    async def test_needed_false_skips_cleanly_without_calling_model(self):
        self.insight["verification"] = {**self.insight["verification"], "needed": False}
        result = await self.execute()
        self.assertEqual((result.status, result.rule), ("skipped", "R5"))
        self.assertEqual(result.reason, "no_check_needed")
        self.assertEqual(result.attempts, 0)

    async def test_accepted_survey_uses_the_decided_window(self):
        result = await self.execute()
        self.assertEqual(result.rule, "R8")
        self.assertEqual(
            result.survey["send_window"],
            {"not_before": "2026-12-06T09:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"},
        )

    def test_changed_prompt_changes_resume_fingerprint(self):
        before = self.bundle.fingerprint(b"input", "us-east-1")
        self.bundle.snapshot["prompts"]["generate"] += " Different instructions."
        self.assertNotEqual(before, self.bundle.fingerprint(b"input", "us-east-1"))

    def test_duplicate_insight_ids_fail_preflight(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_insights(b'{"insight_id":"one"}\n{"insight_id":"one"}')

    def test_null_optional_fields_omitted_for_pack_schema(self):
        draft = SurveyDraft.model_validate(
            {
                "rationale": "Follow up on spraying.",
                "items": [
                    {
                        "item_id": "q1",
                        "type": "survey-confirm",
                        "prompt": "Did you spray?",
                        "options": [
                            {"value": "yes", "label": "Yes"},
                            {"value": "no", "label": "No"},
                        ],
                    }
                ],
            }
        )
        data = draft.model_dump(exclude_none=True)
        self.assertNotIn("follow_up", data["items"][0]["options"][0])

    def test_invalid_scale_rejected(self):
        with self.assertRaises(ValidationError):
            SurveyDraft.model_validate(
                {
                    "rationale": "Test",
                    "items": [
                        {
                            "item_id": "q1",
                            "type": "survey-scale",
                            "prompt": "How much?",
                            "scale_min": 0,
                            "scale_max": 50,
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
