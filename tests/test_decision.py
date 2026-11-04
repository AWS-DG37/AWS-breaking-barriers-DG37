"""Deterministic survey decisions and windows, scored against the golden set offline."""

import copy
import json
import unittest
from datetime import date
from pathlib import Path

from mel.bundle import Bundle
from mel.config import Settings
from mel.decision import DecisionEngine, GoldenWindowPolicy, PackWindowPolicy
from mel.evaluation import insights_jsonl, load_golden, score_decisions

PACK = Path("docs/participant-pack")
GOLDEN = PACK / "data/golden_set.jsonl"


class DecisionRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = Bundle.load(Settings(bedrock_model_id="test-model"))
        cls.engine = cls.bundle.decision_engine()
        cls.base = json.loads(
            (PACK / "json/scenarios/1-mustard-aphid-spray/insight.json").read_text()
        )

    def decide(self, **changes):
        insight = copy.deepcopy(self.base)
        for key, value in changes.items():
            insight[key] = value
        return self.engine.decide(insight)

    def test_worked_example_gets_survey_with_golden_window(self):
        decision = self.decide()
        self.assertEqual((decision.rule, decision.survey), ("R8", True))
        self.assertEqual(
            decision.window.to_contract(),
            {"not_before": "2026-12-06T09:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"},
        )

    def test_junk_flag_and_schema_errors_are_r1(self):
        self.assertEqual(self.decide(quality_flag="junk").rule, "R1")
        broken = copy.deepcopy(self.base)
        del broken["farmer_id"]
        decision = self.engine.decide(broken)
        self.assertEqual((decision.rule, decision.reason_code), ("R1", "junk_insight"))
        self.assertIn("farmer_id", decision.explanation)

    def test_no_advice_resolutions_carry_the_pack_no_survey_code(self):
        info = self.decide(resolution_type="generic_information")
        waiting = self.decide(resolution_type="needs_more_context")
        self.assertEqual((info.rule, info.no_survey_code), ("R3", "generic_information"))
        self.assertEqual((waiting.rule, waiting.no_survey_code), ("R4", "needs_more_context"))

    def test_verification_not_needed_is_r5(self):
        verification = {**self.base["verification"], "needed": False}
        self.assertEqual(self.decide(verification=verification).rule, "R5")

    def test_no_window_before_season_end_is_r6(self):
        self.assertEqual(self.decide(created_at="2027-03-31T09:12:00+05:30").rule, "R6")

    def test_crop_calendar_end_on_advice_day_is_r7(self):
        last_day = self.bundle.calendar().last_day("cauliflower").isoformat()
        asset = {"name": "cauliflower", "category": "crop", "growth_stage": None}
        verification = {**self.base["verification"], "timing": "same_day"}
        on_last_day = self.decide(
            asset=asset,
            verification=verification,
            action_window_days=1,
            created_at=f"{last_day}T09:00:00+05:30",
        )
        self.assertEqual(
            (on_last_day.rule, on_last_day.reason_code), ("R7", "crop_already_harvested")
        )

    def test_engine_requires_a_window_rule(self):
        engine = DecisionEngine([], self.engine._window_policy, lambda insight: [])
        late = copy.deepcopy(self.base)
        late["created_at"] = "2027-03-31T09:12:00+05:30"
        with self.assertRaisesRegex(ValueError, "R6"):
            engine.decide(late)


class WindowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bundle = Bundle.load(Settings(bedrock_model_id="test-model"))
        rules = bundle.snapshot["rules"]
        cls.pack = PackWindowPolicy(rules["send_window_days"], rules["window_rules"])
        cls.golden = GoldenWindowPolicy(
            bundle.config.decision.golden_v1,
            date.fromisoformat(rules["window_rules"]["season_end"]),
        )
        cls.base = json.loads(
            (PACK / "json/scenarios/1-mustard-aphid-spray/insight.json").read_text()
        )

    def insight(self, timing, created_at=None, action_window_days=None):
        insight = copy.deepcopy(self.base)
        insight["verification"]["timing"] = timing
        if created_at:
            insight["created_at"] = created_at
        if action_window_days is not None:
            insight["action_window_days"] = action_window_days
        return insight

    def window(self, policy, insight):
        return policy.window_for(insight).to_contract()

    def test_pack_window_matches_worked_example(self):
        self.assertEqual(
            self.window(self.pack, self.base),
            {"not_before": "2026-12-06T07:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"},
        )

    def test_pack_same_day_respects_action_window(self):
        insight = self.insight("same_day", action_window_days=5)
        self.assertEqual(
            self.window(self.pack, insight),
            {"not_before": "2026-12-09T07:00:00+05:30", "not_after": "2026-12-11T19:00:00+05:30"},
        )

    def test_golden_day_offsets_ignore_action_window(self):
        insight = self.insight("three_to_ten_days", action_window_days=10)
        self.assertEqual(
            self.window(self.golden, insight),
            {"not_before": "2026-12-07T09:00:00+05:30", "not_after": "2026-12-14T19:00:00+05:30"},
        )

    def test_golden_same_day_opens_hours_after_advice(self):
        insight = self.insight("same_day", created_at="2026-12-04T11:53:00+05:30")
        self.assertEqual(
            self.window(self.golden, insight),
            {"not_before": "2026-12-04T15:53:00+05:30", "not_after": "2026-12-04T19:53:00+05:30"},
        )

    def test_golden_same_day_after_hours_moves_to_next_morning(self):
        insight = self.insight("same_day", created_at="2026-12-04T17:17:00+05:30")
        self.assertEqual(
            self.window(self.golden, insight),
            {"not_before": "2026-12-05T09:00:00+05:30", "not_after": "2026-12-05T13:00:00+05:30"},
        )

    def test_golden_window_is_clipped_to_season_end_or_absent_after_it(self):
        clipped = self.insight("three_to_ten_days", created_at="2027-03-28T18:05:00+05:30")
        self.assertEqual(
            self.window(self.golden, clipped),
            {"not_before": "2027-03-31T09:00:00+05:30", "not_after": "2027-03-31T19:00:00+05:30"},
        )
        late = self.insight("three_to_ten_days", created_at="2027-03-30T14:09:00+05:30")
        self.assertIsNone(self.golden.window_for(late))

    def test_golden_policy_rejects_timing_it_has_no_rule_for(self):
        with self.assertRaisesRegex(ValueError, "none"):
            self.golden.window_for(self.insight("none"))


class GoldenDecisionTests(unittest.TestCase):
    # R2 (critical severity -> human call, no survey) was removed on purpose, so the golden
    # set's R2 cases are an intended difference. Every other case must still match exactly.
    @classmethod
    def setUpClass(cls):
        cls.engine = Bundle.load(Settings(bedrock_model_id="test-model")).decision_engine()
        cases = load_golden(GOLDEN)
        cls.r2_cases = [c for c in cases if c.expected["rule"] == "R2"]
        rest = [c for c in cases if c.expected["rule"] != "R2"]
        cls.report = score_decisions(cls.engine, rest).to_dict()

    def test_golden_r2_cases_go_through_the_normal_rules(self):
        self.assertEqual(len(self.r2_cases), 6)
        for case in self.r2_cases:
            decision = self.engine.decide(case.insight)
            self.assertNotEqual(decision.rule, "R2", case.golden_id)
            self.assertNotEqual(decision.reason_code, "critical_needs_human_call", case.golden_id)

    def test_never_surveys_what_the_golden_set_declines(self):
        self.assertEqual(self.report["confusion"]["false_survey"], 0)

    def test_every_decline_rule_matches_the_golden_set(self):
        for rule, counts in self.report["by_expected_rule"].items():
            if rule != "R8":
                self.assertEqual(counts["correct"], counts["cases"], rule)

    def test_every_decision_and_window_matches_the_golden_set(self):
        self.assertEqual(self.report["mismatches"], [])
        self.assertEqual(self.report["window"]["exact_match"], 1.0)

    def test_golden_insight_export_hides_expected_answers(self):
        cases = load_golden(GOLDEN)[:3]
        rows = [json.loads(line) for line in insights_jsonl(cases).splitlines()]
        self.assertEqual(
            [row["insight_id"] for row in rows], [c.insight["insight_id"] for c in cases]
        )
        self.assertTrue(all("expected" not in row for row in rows))


if __name__ == "__main__":
    unittest.main()
