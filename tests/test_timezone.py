"""Timezone handling for timing decisions.

The participant pack's insight contract (insight.schema.json) requires `created_at`
to be India time with an explicit +05:30 offset ("India time, always with the +05:30
offset"); survey.schema.json's `ist_time` requires the same for window bounds. These
tests pin the project's timezone policy:

  * A timezone-aware timestamp (any offset) is converted to IST. This must not depend
    on the machine's local timezone, so the pipeline behaves identically on an IST
    developer laptop and a UTC AWS ECS container.
  * A naive timestamp violates the contract and must be rejected with a clear error,
    never silently reinterpreted against the machine's local timezone.

Stage 1 adds these tests before the implementation. `parse_ist` is the shared helper
introduced in Stage 2; the window-policy tests exercise the real policies and would
have exposed the machine-timezone bug.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import time
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from mel.clock import IST
from mel.config import Settings
from mel.decision import GoldenWindowPolicy, PackWindowPolicy

PACK = Path("docs/participant-pack")


@contextlib.contextmanager
def machine_timezone(tz: str):
    """Temporarily set the process-local timezone, restoring it afterwards.

    Used to prove that timestamp handling does not depend on where the code runs.
    time.tzset() is POSIX-only; the tests that use this are skipped without it.
    """
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


# Timezones chosen to differ from IST on both sides of the clock.
OTHER_ZONES = ("UTC", "America/Los_Angeles", "Asia/Kolkata", "Pacific/Kiritimati")


class ParseIstTests(unittest.TestCase):
    """The shared helper that every timing call site uses to read a timestamp."""

    def setUp(self):
        # Imported lazily so the rest of the suite is collectable before Stage 2 lands.
        from mel.clock import parse_ist

        self.parse_ist = parse_ist

    def test_pack_offset_timestamp_is_returned_in_ist(self):
        parsed = self.parse_ist("2026-12-04T09:12:00+05:30")
        self.assertEqual(parsed.utcoffset(), IST.utcoffset(parsed))
        self.assertEqual(parsed.isoformat(), "2026-12-04T09:12:00+05:30")

    def test_utc_timestamp_is_converted_to_ist(self):
        # 2026-12-04T03:42:00Z == 2026-12-04T09:12:00+05:30
        parsed = self.parse_ist("2026-12-04T03:42:00+00:00")
        self.assertEqual(parsed.utcoffset().total_seconds(), 19800)
        self.assertEqual(parsed.isoformat(), "2026-12-04T09:12:00+05:30")

    def test_z_suffix_is_treated_as_utc(self):
        parsed = self.parse_ist("2026-12-04T03:42:00Z")
        self.assertEqual(parsed.isoformat(), "2026-12-04T09:12:00+05:30")

    def test_accepts_an_existing_datetime_object(self):
        aware = datetime(2026, 12, 4, 3, 42, tzinfo=UTC)
        parsed = self.parse_ist(aware)
        self.assertEqual(parsed.isoformat(), "2026-12-04T09:12:00+05:30")

    def test_naive_string_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse_ist("2026-12-04T09:12:00")

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse_ist(datetime(2026, 12, 4, 9, 12))

    def test_unparseable_value_is_rejected(self):
        with self.assertRaises(ValueError):
            self.parse_ist("not-a-timestamp")

    def test_conversion_is_independent_of_machine_timezone(self):
        results = []
        for tz in OTHER_ZONES:
            with machine_timezone(tz):
                results.append(self.parse_ist("2026-12-04T03:42:00+00:00").isoformat())
        self.assertEqual(set(results), {"2026-12-04T09:12:00+05:30"})

    def test_naive_rejected_regardless_of_machine_timezone(self):
        for tz in OTHER_ZONES:
            with self.subTest(tz=tz), machine_timezone(tz):
                with self.assertRaises(ValueError):
                    self.parse_ist("2026-12-04T09:12:00")


class WindowTimezoneTests(unittest.TestCase):
    """The real window policies must not shift a window based on the host timezone."""

    @classmethod
    def setUpClass(cls):
        from mel.bundle import Bundle

        bundle = Bundle.load(Settings(bedrock_model_id="test-model"))
        rules = bundle.snapshot["rules"]
        cls.pack = PackWindowPolicy(rules["send_window_days"], rules["window_rules"])
        cls.golden = GoldenWindowPolicy(
            bundle.config.decision.golden_v1,
            date.fromisoformat(rules["window_rules"]["season_end"]),
        )
        scenario = PACK / "json/scenarios/1-mustard-aphid-spray/insight.json"
        cls.base = json.loads(scenario.read_text())

    def insight(self, **changes):
        insight = copy.deepcopy(self.base)
        insight.update(changes)
        return insight

    def test_golden_window_identical_across_machine_timezones(self):
        insight = self.insight(created_at="2026-12-04T09:12:00+05:30")
        windows = []
        for tz in OTHER_ZONES:
            with machine_timezone(tz):
                windows.append(self.golden.window_for(insight).to_contract())
        self.assertEqual(
            windows,
            [{"not_before": "2026-12-06T09:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"}]
            * len(OTHER_ZONES),
        )

    def test_pack_window_identical_across_machine_timezones(self):
        insight = self.insight(created_at="2026-12-04T09:12:00+05:30")
        windows = []
        for tz in OTHER_ZONES:
            with machine_timezone(tz):
                windows.append(self.pack.window_for(insight).to_contract())
        self.assertEqual(
            windows,
            [{"not_before": "2026-12-06T07:00:00+05:30", "not_after": "2026-12-09T19:00:00+05:30"}]
            * len(OTHER_ZONES),
        )

    def test_utc_created_at_yields_same_window_as_equivalent_ist(self):
        # 2026-12-04T03:42:00Z is the same instant as 2026-12-04T09:12:00+05:30.
        utc = self.golden.window_for(self.insight(created_at="2026-12-04T03:42:00+00:00"))
        ist = self.golden.window_for(self.insight(created_at="2026-12-04T09:12:00+05:30"))
        self.assertEqual(utc.to_contract(), ist.to_contract())

    def test_near_midnight_utc_does_not_shift_the_day_in_ist(self):
        # 2026-12-04T20:00:00Z == 2026-12-05T01:30:00+05:30: the IST calendar day is the 5th,
        # so a day-offset window must count from the 5th, not the 4th.
        insight = self.insight(
            created_at="2026-12-04T20:00:00+00:00",
            verification={**self.base["verification"], "timing": "next_day"},
        )
        for tz in OTHER_ZONES:
            with self.subTest(tz=tz), machine_timezone(tz):
                window = self.golden.window_for(insight).to_contract()
                self.assertEqual(window["not_before"], "2026-12-06T09:00:00+05:30")
                self.assertEqual(window["not_after"], "2026-12-07T19:00:00+05:30")

    def test_naive_created_at_is_rejected_by_window_policies(self):
        naive = self.insight(created_at="2026-12-04T09:12:00")
        with self.assertRaises(ValueError):
            self.golden.window_for(naive)
        with self.assertRaises(ValueError):
            self.pack.window_for(naive)


if __name__ == "__main__":
    unittest.main()
