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

    def test_golden_policy_guards_against_timing_it_has_no_rule_for(self):
        # Defensive guard. The engine now declines such insights at R5b before the
        # window is ever computed (see VerificationMetadataTests), so this raise
        # should be unreachable in the live pipeline; it stays as a backstop.
        with self.assertRaisesRegex(ValueError, "none"):
            self.golden.window_for(self.insight("none"))


class WindowBoundaryTests(unittest.TestCase):
    """Boundary characterization for both policies.

    These pin the behavior that the golden set and the pack's written rules require,
    so a later refactor cannot silently move a boundary. They assert the *current*
    contract; none of them demanded a production change in Stage 4. Two facts worth
    stating because they look surprising but are correct:

      * golden same_day windows may END after 19:00 (e.g. advice at 15:53 -> ends
        19:53). The golden set itself contains such windows, and the 07:00-19:00
        daily limit is the scheduler's send-hours concern (handoff.send_hours),
        carried separately from the window's not_after. See
        test_golden_same_day_window_may_end_after_send_hours.
      * golden ignores action_window_days entirely; pack applies it. This is the
        documented golden-vs-pack difference, not a bug.
    """

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

    def insight(self, timing, created_at=None, action_window_days="keep"):
        """Build an insight. `action_window_days` accepts None and 0 explicitly;
        the sentinel "keep" leaves the base value (2) untouched."""
        insight = copy.deepcopy(self.base)
        insight["verification"]["timing"] = timing
        if created_at:
            insight["created_at"] = created_at
        if action_window_days != "keep":
            insight["action_window_days"] = action_window_days
        return insight

    def window(self, policy, insight):
        result = policy.window_for(insight)
        return result.to_contract() if result is not None else None

    # --- A. same_day (golden) ------------------------------------------------

    def test_golden_same_day_before_opening_hours(self):
        # Advice 06:00; delay 4h -> 10:00, inside 09:00-19:00, used as-is.
        self.assertEqual(
            self.window(self.golden, self.insight("same_day", "2026-12-04T06:00:00+05:30")),
            {"not_before": "2026-12-04T10:00:00+05:30", "not_after": "2026-12-04T14:00:00+05:30"},
        )

    def test_golden_same_day_during_sending_hours(self):
        self.assertEqual(
            self.window(self.golden, self.insight("same_day", "2026-12-04T11:53:00+05:30")),
            {"not_before": "2026-12-04T15:53:00+05:30", "not_after": "2026-12-04T19:53:00+05:30"},
        )

    def test_golden_same_day_advice_exactly_at_closing_time_moves_to_next_morning(self):
        # Advice exactly 19:00; delay -> 23:00, strictly after close -> next 09:00.
        self.assertEqual(
            self.window(self.golden, self.insight("same_day", "2026-12-04T19:00:00+05:30")),
            {"not_before": "2026-12-05T09:00:00+05:30", "not_after": "2026-12-05T13:00:00+05:30"},
        )

    def test_golden_same_day_after_closing_time_moves_to_next_morning(self):
        self.assertEqual(
            self.window(self.golden, self.insight("same_day", "2026-12-04T17:17:00+05:30")),
            {"not_before": "2026-12-05T09:00:00+05:30", "not_after": "2026-12-05T13:00:00+05:30"},
        )

    def test_golden_same_day_opening_exactly_at_close_is_allowed(self):
        # Advice 15:00; delay -> 19:00. 19:00 is not strictly after close, so it opens
        # at 19:00 the same day rather than deferring. Pins the > (not >=) boundary.
        self.assertEqual(
            self.window(self.golden, self.insight("same_day", "2026-12-04T15:00:00+05:30")),
            {"not_before": "2026-12-04T19:00:00+05:30", "not_after": "2026-12-04T23:00:00+05:30"},
        )

    def test_golden_same_day_delay_crossing_midnight_defers_to_next_morning(self):
        self.assertEqual(
            self.window(self.golden, self.insight("same_day", "2026-12-04T23:30:00+05:30")),
            {"not_before": "2026-12-05T09:00:00+05:30", "not_after": "2026-12-05T13:00:00+05:30"},
        )

    def test_golden_same_day_window_may_end_after_send_hours(self):
        # Contract fact: the window END can exceed 19:00. The golden set contains such
        # rows (e.g. 2027-01-03 ends 19:53). The daily 07:00-19:00 restriction lives in
        # the handoff's send_hours, not in the window bounds.
        window = self.window(self.golden, self.insight("same_day", "2026-12-04T14:30:00+05:30"))
        self.assertEqual(window["not_before"], "2026-12-04T18:30:00+05:30")
        self.assertEqual(window["not_after"], "2026-12-04T22:30:00+05:30")

    def test_golden_same_day_clipped_near_season_end(self):
        # Opening still inside the last season day; the 4-hour length does not extend
        # the season, and the opening is before season close, so a window exists.
        self.assertEqual(
            self.window(self.golden, self.insight("same_day", "2027-03-31T08:00:00+05:30")),
            {"not_before": "2027-03-31T12:00:00+05:30", "not_after": "2027-03-31T16:00:00+05:30"},
        )

    def test_golden_same_day_deferred_opening_past_season_has_no_window(self):
        # Advice late on the last day defers the opening to 2027-04-01, past season.
        late = self.insight("same_day", "2027-03-31T16:00:00+05:30")
        self.assertIsNone(self.golden.window_for(late))

    # --- B. action_window_days (pack applies it; golden ignores it) ----------

    def test_pack_null_action_window_uses_configured_default(self):
        # action_window_days null -> treated as 2 (action_window_days_if_null).
        self.assertEqual(
            self.window(self.pack, self.insight("two_to_five_days", action_window_days=None)),
            {"not_before": "2026-12-06T07:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"},
        )

    def test_pack_zero_action_window_still_respects_earliest_start(self):
        # awd=0 -> start_not_before_action_window_end has no effect, earliest_start_day=1 wins.
        self.assertEqual(
            self.window(self.pack, self.insight("two_to_five_days", action_window_days=0)),
            {"not_before": "2026-12-06T07:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"},
        )

    def test_pack_action_window_longer_than_timing_range_pushes_start(self):
        # awd=10 overrides the timing's start; min_width keeps a 2-day span.
        self.assertEqual(
            self.window(self.pack, self.insight("two_to_five_days", action_window_days=10)),
            {"not_before": "2026-12-14T07:00:00+05:30", "not_after": "2026-12-16T19:00:00+05:30"},
        )

    def test_pack_action_window_beyond_season_has_no_window(self):
        self.assertIsNone(
            self.pack.window_for(self.insight("two_to_five_days", action_window_days=180))
        )

    def test_golden_ignores_action_window_days_entirely(self):
        # Documented golden-vs-pack difference: awd has no effect on golden windows.
        default = self.window(self.golden, self.insight("two_to_five_days"))
        for awd in (None, 0, 10, 180):
            with self.subTest(action_window_days=awd):
                insight = self.insight("two_to_five_days", action_window_days=awd)
                self.assertEqual(self.window(self.golden, insight), default)

    # --- C. seasonal boundaries ----------------------------------------------

    def test_golden_window_entirely_before_season_end(self):
        insight = self.insight("three_to_ten_days", "2026-12-04T09:12:00+05:30")
        self.assertEqual(
            self.window(self.golden, insight),
            {"not_before": "2026-12-07T09:00:00+05:30", "not_after": "2026-12-14T19:00:00+05:30"},
        )

    def test_golden_window_ending_exactly_on_season_end(self):
        # last offset lands exactly on 2027-03-31; no clipping needed.
        insight = self.insight("three_to_ten_days", "2027-03-21T09:00:00+05:30")
        self.assertEqual(
            self.window(self.golden, insight),
            {"not_before": "2027-03-24T09:00:00+05:30", "not_after": "2027-03-31T19:00:00+05:30"},
        )

    def test_golden_window_partially_beyond_season_is_clipped(self):
        insight = self.insight("three_to_ten_days", "2027-03-28T18:05:00+05:30")
        self.assertEqual(
            self.window(self.golden, insight),
            {"not_before": "2027-03-31T09:00:00+05:30", "not_after": "2027-03-31T19:00:00+05:30"},
        )

    def test_golden_window_opening_after_season_is_absent(self):
        self.assertIsNone(
            self.golden.window_for(self.insight("three_to_ten_days", "2027-03-30T14:09:00+05:30"))
        )

    def test_pack_partially_beyond_season_fails_min_width(self):
        # pack clips latest to season end; if the clipped span < min_width_days -> None.
        self.assertIsNone(
            self.pack.window_for(self.insight("three_to_ten_days", "2027-03-28T18:05:00+05:30"))
        )

    def test_pack_tight_season_below_min_width_has_no_window(self):
        self.assertIsNone(
            self.pack.window_for(self.insight("two_to_five_days", "2027-03-30T09:00:00+05:30"))
        )

    # --- D. send hours vs send window ----------------------------------------

    def test_pack_window_bounds_use_configured_open_and_close_times(self):
        window = self.window(self.pack, self.insight("two_to_five_days"))
        self.assertTrue(window["not_before"].endswith("07:00:00+05:30"))
        self.assertTrue(window["not_after"].endswith("19:00:00+05:30"))

    def test_golden_day_window_bounds_use_configured_open_and_close_times(self):
        window = self.window(self.golden, self.insight("two_to_five_days"))
        self.assertTrue(window["not_before"].endswith("09:00:00+05:30"))
        self.assertTrue(window["not_after"].endswith("19:00:00+05:30"))


class VerificationMetadataTests(unittest.TestCase):
    """How the engine treats the `verification` block, including the cases that must
    never reach a window policy as an unhandled exception.

    The key distinction: `needed=false` is a clean "no check needed" (R5); metadata
    that is self-contradictory or malformed is a separate rejection, not R5. Schema-
    breaking rows (null/missing/unknown enum values) are caught by R1 because they
    fail the insight schema; the one schema-valid but contradictory combination,
    `needed=true` with `timing="none"`, is caught by R5b.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = Bundle.load(Settings(bedrock_model_id="test-model")).decision_engine()
        cls.base = json.loads(
            (PACK / "json/scenarios/1-mustard-aphid-spray/insight.json").read_text()
        )

    def decide(self, verification):
        insight = copy.deepcopy(self.base)
        if verification is None:
            insight.pop("verification", None)
        else:
            insight["verification"] = verification
        return self.engine.decide(insight)

    def _verification(self, **changes):
        return {**self.base["verification"], **changes}

    # needed=false -> clean R5 ------------------------------------------------

    def test_needed_false_is_clean_no_check_needed(self):
        decision = self.decide(self._verification(needed=False))
        self.assertEqual((decision.rule, decision.survey), ("R5", False))
        self.assertEqual(decision.reason_code, "no_check_needed")

    def test_needed_false_wins_even_with_none_timing(self):
        # needed=false is unambiguous; it is not reclassified as inconsistent.
        decision = self.decide(self._verification(needed=False, timing="none"))
        self.assertEqual((decision.rule, decision.reason_code), ("R5", "no_check_needed"))

    # needed=true + timing=none -> R5b (schema-valid but contradictory) -------

    def test_needed_true_with_none_timing_is_invalid_not_no_check(self):
        decision = self.decide(self._verification(needed=True, timing="none"))
        self.assertEqual((decision.rule, decision.survey), ("R5b", False))
        self.assertEqual(decision.reason_code, "invalid_verification_timing")
        self.assertNotEqual(decision.reason_code, "no_check_needed")

    def test_inconsistent_metadata_produces_no_survey_object_free_decision(self):
        decision = self.decide(self._verification(needed=True, timing="none"))
        # R5b is not one of the pack's no_survey contract reasons (only R3/R4 are).
        self.assertIsNone(decision.no_survey_code)

    # malformed metadata -> R1 via schema (not an unhandled exception) --------

    def test_null_timing_is_declined_without_exception(self):
        decision = self.decide(self._verification(needed=True, timing=None))
        self.assertFalse(decision.survey)
        self.assertEqual(decision.rule, "R1")

    def test_missing_timing_is_declined_without_exception(self):
        verification = self._verification(needed=True)
        verification.pop("timing", None)
        decision = self.decide(verification)
        self.assertFalse(decision.survey)

    def test_unknown_timing_is_declined_without_exception(self):
        decision = self.decide(self._verification(needed=True, timing="whenever"))
        self.assertFalse(decision.survey)

    def test_null_needed_is_not_treated_as_no_check_needed(self):
        decision = self.decide(self._verification(needed=None))
        self.assertFalse(decision.survey)
        self.assertNotEqual(decision.reason_code, "no_check_needed")

    def test_missing_needed_is_not_treated_as_no_check_needed(self):
        verification = self._verification()
        verification.pop("needed", None)
        decision = self.decide(verification)
        self.assertFalse(decision.survey)
        self.assertNotEqual(decision.reason_code, "no_check_needed")

    def test_missing_verification_block_is_declined_without_exception(self):
        decision = self.decide(None)
        self.assertFalse(decision.survey)

    # no bad timing ever reaches the window policy ----------------------------

    def test_engine_never_raises_for_any_schema_timing_value(self):
        # Every value the insight schema allows for timing, including "none", must
        # produce a Decision rather than propagate an exception from the window code.
        for timing in (
            "none",
            "same_day",
            "next_day",
            "two_to_five_days",
            "three_to_ten_days",
            "seven_to_fourteen_days",
            "seasonal",
        ):
            with self.subTest(timing=timing):
                decision = self.decide(self._verification(needed=True, timing=timing))
                self.assertIsNotNone(decision.rule)


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
