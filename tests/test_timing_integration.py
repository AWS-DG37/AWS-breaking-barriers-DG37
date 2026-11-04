"""Integration tests for the survey timing path, end to end.

These exercise the real decision engine and the real survey graph (with TestModel
agents in place of Bedrock), then assert that the window the decision engine computes
is the exact window carried by the final scheduler handoff - no truncation, re-parsing,
or timezone drift between calculation and handoff.

Scope: timing-window correctness and faithful handoff passthrough only. Scheduler
enforcement (daily send-hours, retries, caps) is out of scope and not tested here.

Expected timestamps come from the configured production policy (golden_v1) and the
values already pinned in test_decision.py / the golden set, never invented.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import time
import unittest
from datetime import datetime
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from mel.bundle import Bundle
from mel.clock import IST
from mel.config import Settings
from mel.graph import Dependencies, Select, State, survey_graph
from mel.handoff import handoff
from mel.models import Review, SurveyDraft

PACK = Path("docs/participant-pack")
SCENARIO = PACK / "json/scenarios/1-mustard-aphid-spray"

# A fixed "now" for handoff created_at, well inside sending hours, tz-aware.
NOW = datetime(2026, 12, 4, 23, 0, 0, tzinfo=IST)


@contextlib.contextmanager
def machine_timezone(tz: str):
    if not hasattr(time, "tzset"):
        raise unittest.SkipTest("time.tzset is unavailable on this platform")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class TimingPipelineTests(unittest.IsolatedAsyncioTestCase):
    """The full Select -> Generate -> Review -> handoff path for an accepted survey."""

    def setUp(self):
        self.bundle = Bundle.load(Settings(bedrock_model_id="test-model"))
        self.base = json.loads((SCENARIO / "insight.json").read_text())
        survey = json.loads((SCENARIO / "survey.hi-IN.json").read_text())
        # main's SurveyDraft constrains option values to a fixed answer-code enum, so
        # shape the pack survey into an enum-valid draft (same approach as test_graph.py).
        survey["items"] = [survey["items"][0], survey["items"][2]]
        survey["items"][1]["options"] = [
            {"value": "better", "label": "पहले से कम"},
            {"value": "same", "label": "पहले जितने"},
            {"value": "worse", "label": "पहले से ज़्यादा", "follow_up": "crop_or_animal_damaged"},
        ]
        draft = {"items": survey["items"], "rationale": survey["rationale"]}
        review = {
            "relevance_score": 0.95,
            "language_correct": True,
            "gives_new_advice": False,
            "asks_personal_info": False,
            "issues": [],
        }
        self.deps = Dependencies.from_bundle(
            self.bundle,
            Agent(TestModel(custom_output_args=draft), output_type=SurveyDraft),
            Agent(TestModel(custom_output_args=review), output_type=Review),
        )

    # -- helpers --------------------------------------------------------------

    def insight(self, **changes):
        insight = copy.deepcopy(self.base)
        for key, value in changes.items():
            if key == "timing":
                insight["verification"] = {**insight["verification"], "timing": value}
            else:
                insight[key] = value
        return insight

    async def run_graph(self, insight):
        state = State(insight, "S-0000000001", NOW)
        return (await survey_graph.run(Select(), state=state, deps=self.deps)).output

    def engine_window(self, insight):
        return self.bundle.decision_engine().decide(insight).window.to_contract()

    async def assert_window(self, insight, not_before, not_after):
        """The engine, the accepted survey, and the handoff must all carry the same
        window, and it must equal the expected bounds."""
        expected = {"not_before": not_before, "not_after": not_after}
        self.assertEqual(self.engine_window(insight), expected)
        result = await self.run_graph(insight)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.survey["send_window"], expected)
        self.assertEqual(result.handoff["send_window"], expected)
        # The handoff validates against the pack schema (offsets, formats included).
        self.bundle.contracts.validate("schedule_handoff", result.handoff)
        return result

    # -- representative timing cases ------------------------------------------

    async def test_normal_multi_day_window(self):
        # golden_v1 two_to_five_days from 2026-12-04 advice -> [+2, +5] at 09:00/19:00.
        await self.assert_window(
            self.insight(timing="two_to_five_days"),
            "2026-12-06T09:00:00+05:30",
            "2026-12-09T19:00:00+05:30",
        )

    async def test_same_day_window_during_hours(self):
        # Advice 11:53; delay 4h -> opens 15:53, lasts 4h -> 19:53 (end past 19:00 is
        # intentional; send_hours handles the daily limit separately).
        result = await self.assert_window(
            self.insight(timing="same_day", created_at="2026-12-04T11:53:00+05:30"),
            "2026-12-04T15:53:00+05:30",
            "2026-12-04T19:53:00+05:30",
        )
        # send_hours is the daily restriction, carried verbatim from the rules and
        # distinct from the window end.
        self.assertEqual(result.handoff["send_hours"], self.bundle.snapshot["rules"]["send_hours"])

    async def test_advice_before_opening_time(self):
        # same_day advice 06:00; delay -> 10:00, inside 09:00-19:00, used as-is.
        await self.assert_window(
            self.insight(timing="same_day", created_at="2026-12-04T06:00:00+05:30"),
            "2026-12-04T10:00:00+05:30",
            "2026-12-04T14:00:00+05:30",
        )

    async def test_advice_near_daily_closing_time_defers_to_next_morning(self):
        # same_day advice 17:17; delay -> 21:17, after close -> next day 09:00.
        await self.assert_window(
            self.insight(timing="same_day", created_at="2026-12-04T17:17:00+05:30"),
            "2026-12-05T09:00:00+05:30",
            "2026-12-05T13:00:00+05:30",
        )

    async def test_window_clipped_at_season_boundary(self):
        # three_to_ten_days advice 2027-03-28 -> [+3,+10] clipped to season end 03-31.
        await self.assert_window(
            self.insight(timing="three_to_ten_days", created_at="2027-03-28T18:05:00+05:30"),
            "2027-03-31T09:00:00+05:30",
            "2027-03-31T19:00:00+05:30",
        )

    async def test_advice_near_season_end_with_no_window_is_skipped(self):
        # Opening would fall after the season; R6 declines before any model call.
        insight = self.insight(
            timing="three_to_ten_days", created_at="2027-03-30T14:09:00+05:30"
        )
        result = await self.run_graph(insight)
        self.assertEqual((result.status, result.rule), ("skipped", "R6"))
        self.assertEqual(result.reason, "window_after_season")
        self.assertIsNone(result.handoff)

    # -- UTC -> IST and the insight contract ----------------------------------

    async def test_window_policy_converts_utc_created_at_to_identical_ist_window(self):
        # The insight contract requires created_at to carry +05:30, so a UTC string is
        # a schema-invalid insight at the engine level (see the R1 test below). The
        # timezone conversion itself lives in the window policy, which must yield the
        # same instant-based window for UTC input as for the equivalent IST input.
        # 2026-12-04T03:42:00Z == 2026-12-04T09:12:00+05:30 (the scenario's advice time).
        policy = self.deps.decisions._window_policy
        utc = policy.window_for(
            self.insight(timing="two_to_five_days", created_at="2026-12-04T03:42:00+00:00")
        )
        ist = policy.window_for(
            self.insight(timing="two_to_five_days", created_at="2026-12-04T09:12:00+05:30")
        )
        self.assertEqual(utc.to_contract(), ist.to_contract())
        self.assertEqual(
            utc.to_contract(),
            {"not_before": "2026-12-06T09:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"},
        )

    async def test_non_ist_offset_insight_is_schema_rejected_at_r1(self):
        # The insight schema's created_at pattern mandates +05:30; a +00:00 timestamp
        # is therefore a malformed insight, declined as junk (R1) before any window or
        # model call - not silently surveyed with a converted window.
        insight = self.insight(timing="two_to_five_days", created_at="2026-12-04T03:42:00+00:00")
        result = await self.run_graph(insight)
        self.assertEqual((result.status, result.rule), ("skipped", "R1"))
        self.assertIsNone(result.handoff)

    async def test_every_emitted_handoff_timestamp_is_ist(self):
        result = await self.run_graph(self.insight(timing="two_to_five_days"))
        self.assertEqual(result.status, "accepted")
        for value in (
            result.handoff["send_window"]["not_before"],
            result.handoff["send_window"]["not_after"],
            result.handoff["created_at"],
            result.handoff["expires_at"],
        ):
            self.assertTrue(value.endswith("+05:30"), value)

    async def test_window_is_independent_of_machine_timezone(self):
        insight = self.insight(timing="two_to_five_days")
        windows = []
        for tz in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
            with machine_timezone(tz):
                windows.append(self.engine_window(insight))
        self.assertEqual(
            windows,
            [{"not_before": "2026-12-06T09:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"}]
            * 3,
        )

    # -- invalid / missing metadata and timestamps ----------------------------

    async def test_inconsistent_timing_metadata_skips_before_handoff(self):
        insight = self.insight()
        insight["verification"] = {**insight["verification"], "needed": True, "timing": "none"}
        result = await self.run_graph(insight)
        self.assertEqual((result.status, result.rule), ("skipped", "R5b"))
        self.assertEqual(result.reason, "invalid_verification_timing")
        self.assertIsNone(result.handoff)

    async def test_missing_created_at_is_a_failure_not_a_wrong_window(self):
        insight = self.insight()
        del insight["created_at"]
        result = await self.run_graph(insight)
        # No handoff and no survey window are produced from a missing timestamp.
        self.assertNotEqual(result.status, "accepted")
        self.assertIsNone(result.handoff)

    async def test_naive_created_at_is_rejected_not_silently_localized(self):
        insight = self.insight(created_at="2026-12-04T09:12:00")  # no offset
        result = await self.run_graph(insight)
        self.assertNotEqual(result.status, "accepted")
        self.assertIsNone(result.handoff)

    # -- handoff passthrough fidelity -----------------------------------------

    async def test_handoff_window_is_byte_identical_to_engine_window(self):
        insight = self.insight(timing="seven_to_fourteen_days")
        engine = self.engine_window(insight)
        result = await self.run_graph(insight)
        self.assertEqual(result.handoff["send_window"], engine)
        self.assertEqual(result.survey["send_window"], engine)

    async def test_expiry_is_not_after_plus_configured_days(self):
        from datetime import timedelta

        rules = self.bundle.snapshot["rules"]
        insight = self.insight(timing="two_to_five_days")
        result = await self.run_graph(insight)
        not_after = datetime.fromisoformat(result.handoff["send_window"]["not_after"])
        expires = datetime.fromisoformat(result.handoff["expires_at"])
        self.assertEqual(expires, not_after + timedelta(days=rules["expiry_days_after_window"]))
        self.assertEqual(expires.utcoffset().total_seconds(), 19800)  # +05:30


class HandoffNowTimezoneTests(unittest.TestCase):
    """handoff() created_at: aware input converts to IST; naive input is rejected,
    never interpreted against the machine timezone."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = Bundle.load(Settings(bedrock_model_id="test-model"))
        cls.rules = cls.bundle.snapshot["rules"]
        cls.insight = json.loads((SCENARIO / "insight.json").read_text())
        expected = json.loads((SCENARIO / "schedule_handoff.json").read_text())
        cls.survey = expected["survey"]

    def test_utc_now_becomes_ist_created_at(self):
        now_utc = datetime.fromisoformat("2026-12-04T17:30:00+00:00")  # 23:00 IST
        result = handoff(self.survey, self.insight, self.rules, now_utc)
        self.assertEqual(result["created_at"], "2026-12-04T23:00:00+05:30")

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            handoff(self.survey, self.insight, self.rules, datetime(2026, 12, 4, 23, 0, 0))

    def test_naive_now_rejected_regardless_of_machine_timezone(self):
        for tz in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
            with self.subTest(tz=tz), machine_timezone(tz):
                with self.assertRaises(ValueError):
                    handoff(self.survey, self.insight, self.rules, datetime(2026, 12, 4, 23))


if __name__ == "__main__":
    unittest.main()
