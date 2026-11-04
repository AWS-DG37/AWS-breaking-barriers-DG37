"""Unit tests for src/mel/handoff.py.

Every expected value is derived from the production rules (rules.json) and the
participant-pack scenario handoffs so the tests break if either the implementation
or the rules change in an incompatible way.  No mock objects are used: handoff()
is a pure function and can be called directly.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from mel.clock import IST
from mel.handoff import handoff

# ---------------------------------------------------------------------------
# Shared fixtures loaded once for the whole module
# ---------------------------------------------------------------------------

PACK = Path("docs/participant-pack")
RULES: dict = json.loads((PACK / "json/rules/rules.json").read_text())

_SCENARIO_ROOT = PACK / "json/scenarios"


def _load(scenario: str) -> tuple[dict, dict, dict]:
    """Return (insight, survey, expected_handoff) for one worked scenario."""
    d = _SCENARIO_ROOT / scenario
    insight = json.loads((d / "insight.json").read_text(encoding="utf-8"))
    expected = json.loads((d / "schedule_handoff.json").read_text(encoding="utf-8"))
    survey = expected["survey"]
    return insight, survey, expected


# Five worked scenarios from the participant pack.
MUSTARD = _load("1-mustard-aphid-spray")      # normal priority, 2-channel
WHEAT_IRR = _load("2-wheat-irrigation-timing") # normal priority, 1-channel (whatsapp only)
POTATO = _load("3-potato-blight-wait")         # normal priority, 3-channel
COW = _load("4-cow-vet-escalation")            # HIGH priority, escalate_to_expert + emergency
WHEAT_PLAN = _load("5-wheat-variety-plan")     # LOW priority, plan_created


def _handoff(scenario: tuple, now: datetime) -> dict:
    insight, survey, _ = scenario
    return handoff(survey, insight, RULES, now)


def _scenario_now(scenario: tuple) -> datetime:
    """Return the creation timestamp used in the worked scenario."""
    _, _, expected = scenario
    return datetime.fromisoformat(expected["created_at"])


# ---------------------------------------------------------------------------
# 1. Priority calculation
# ---------------------------------------------------------------------------


class PriorityTests(unittest.TestCase):
    """handoff() determines priority from three independent signal paths."""

    def _insight_with(self, **overrides) -> dict:
        insight, _, _ = MUSTARD  # baseline: direct_action, medium severity, diagnosing
        return {**insight, **overrides}

    def _survey(self) -> dict:
        _, survey, _ = MUSTARD
        return survey

    def _call(self, insight: dict) -> dict:
        return handoff(self._survey(), insight, RULES, datetime.now(IST))

    # resolution_type path -------------------------------------------------

    def test_escalate_to_expert_is_high_priority(self):
        result = self._call(self._insight_with(resolution_type="escalate_to_expert"))
        self.assertEqual(result["priority"], "high")

    def test_plan_created_is_low_priority(self):
        result = self._call(self._insight_with(resolution_type="plan_created"))
        self.assertEqual(result["priority"], "low")

    def test_do_not_act_is_low_priority(self):
        result = self._call(self._insight_with(resolution_type="do_not_act"))
        self.assertEqual(result["priority"], "low")

    def test_direct_action_is_normal_priority_by_default(self):
        result = self._call(self._insight_with(resolution_type="direct_action", severity="medium",
                                               behavioral_intent="diagnosing"))
        self.assertEqual(result["priority"], "normal")

    def test_wait_and_monitor_is_normal_priority(self):
        result = self._call(self._insight_with(resolution_type="wait_and_monitor"))
        self.assertEqual(result["priority"], "normal")

    # severity path --------------------------------------------------------

    def test_high_severity_overrides_normal_resolution_to_high_priority(self):
        result = self._call(self._insight_with(severity="high", resolution_type="direct_action"))
        self.assertEqual(result["priority"], "high")

    def test_low_severity_alone_does_not_change_normal_priority(self):
        result = self._call(self._insight_with(severity="low", resolution_type="direct_action"))
        self.assertEqual(result["priority"], "normal")

    # behavioral_intent path -----------------------------------------------

    def test_emergency_intent_overrides_normal_resolution_to_high_priority(self):
        result = self._call(
            self._insight_with(behavioral_intent="emergency", resolution_type="direct_action",
                               severity="medium")
        )
        self.assertEqual(result["priority"], "high")

    def test_procedural_guidance_intent_is_normal_priority(self):
        result = self._call(
            self._insight_with(behavioral_intent="procedural_guidance",
                               resolution_type="direct_action", severity="medium")
        )
        self.assertEqual(result["priority"], "normal")

    # worked-scenario assertions -------------------------------------------

    def test_cow_escalation_scenario_is_high_priority(self):
        result = _handoff(COW, _scenario_now(COW))
        self.assertEqual(result["priority"], "high")

    def test_wheat_plan_scenario_is_low_priority(self):
        result = _handoff(WHEAT_PLAN, _scenario_now(WHEAT_PLAN))
        self.assertEqual(result["priority"], "low")

    def test_mustard_scenario_is_normal_priority(self):
        result = _handoff(MUSTARD, _scenario_now(MUSTARD))
        self.assertEqual(result["priority"], "normal")


# ---------------------------------------------------------------------------
# 2. Channel selection
# ---------------------------------------------------------------------------


class ChannelSelectionTests(unittest.TestCase):
    """Channel order follows: preferred → method channel → whatsapp → (voice if high)."""

    def _call(self, insight: dict, survey: dict | None = None) -> dict:
        if survey is None:
            _, survey, _ = MUSTARD
        return handoff(survey, insight, RULES, datetime.now(IST))

    def _channels(self, result: dict) -> list[str]:
        return [entry["channel"] for entry in result["channel_order"]]

    # preferred channel always first ---------------------------------------

    def test_preferred_channel_is_first(self):
        insight, survey, _ = MUSTARD  # preferred_channel = "app"
        result = self._call(insight, survey)
        self.assertEqual(self._channels(result)[0], "app")

    def test_preferred_channel_voice_is_first_for_cow_escalation(self):
        insight, survey, _ = COW  # preferred_channel = "voice"
        result = self._call(insight, survey)
        self.assertEqual(self._channels(result)[0], "voice")

    # verification method channel ------------------------------------------

    def test_push_method_maps_to_app(self):
        # Mustard: method=push → app; preferred_channel=app → deduped to one entry before whatsapp
        insight, survey, _ = MUSTARD
        result = self._call(insight, survey)
        # Both preferred and method resolve to "app" → appears once
        self.assertEqual(self._channels(result).count("app"), 1)

    def test_whatsapp_method_maps_to_whatsapp(self):
        # Wheat irrigation: preferred_channel=whatsapp, method=whatsapp → single channel
        insight, survey, _ = WHEAT_IRR
        result = self._call(insight, survey)
        self.assertEqual(self._channels(result), ["whatsapp"])

    def test_repeat_photo_maps_to_app_channel(self):
        # Potato: preferred=web, method=repeat_photo → app; both + whatsapp = [web, app, whatsapp]
        insight, survey, _ = POTATO
        result = self._call(insight, survey)
        self.assertIn("app", self._channels(result))
        self.assertEqual(self._channels(result)[0], "web")

    def test_ivr_callback_maps_to_voice_channel(self):
        # Cow: method=ivr_callback → voice; preferred already voice → deduped
        insight, survey, _ = COW
        result = self._call(insight, survey)
        self.assertIn("voice", self._channels(result))

    # whatsapp fallback ----------------------------------------------------

    def test_whatsapp_always_present_for_normal_priority(self):
        insight, survey, _ = MUSTARD
        result = self._call(insight, survey)
        self.assertIn("whatsapp", self._channels(result))

    def test_whatsapp_always_present_for_low_priority(self):
        insight, survey, _ = WHEAT_PLAN
        result = self._call(insight, survey)
        self.assertIn("whatsapp", self._channels(result))

    # deduplication --------------------------------------------------------

    def test_no_duplicate_channels(self):
        for scenario in (MUSTARD, WHEAT_IRR, POTATO, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                insight, survey, _ = scenario
                channels = self._channels(self._call(insight, survey))
                self.assertEqual(len(channels), len(set(channels)))

    def test_channel_order_preserves_insertion_order_after_dedup(self):
        # Wheat plan: preferred=telegram, method=push→app, then whatsapp
        insight, survey, _ = WHEAT_PLAN
        result = self._call(insight, survey)
        channels = self._channels(result)
        self.assertEqual(channels[0], "telegram")
        self.assertEqual(channels[-1], "whatsapp")

    # high-priority voice addition -----------------------------------------

    def test_voice_appended_for_high_priority_when_not_already_present(self):
        # Force high priority on a non-voice insight; voice must appear at the end.
        insight, survey, _ = MUSTARD           # preferred=app, method=push→app, severity=medium
        insight_high = {**insight, "severity": "high"}
        result = self._call(insight_high, survey)
        channels = self._channels(result)
        self.assertIn("voice", channels)
        self.assertEqual(channels[-1], "voice")

    def test_voice_not_duplicated_when_already_preferred_at_high_priority(self):
        # Cow: voice is already preferred; must still appear exactly once.
        insight, survey, _ = COW
        result = self._call(insight, survey)
        self.assertEqual(self._channels(result).count("voice"), 1)

    def test_voice_not_added_for_normal_priority(self):
        insight, survey, _ = MUSTARD
        result = self._call(insight, survey)
        self.assertNotIn("voice", self._channels(result))

    def test_voice_not_added_for_low_priority(self):
        insight, survey, _ = WHEAT_PLAN
        result = self._call(insight, survey)
        self.assertNotIn("voice", self._channels(result))

    # worked-scenario channel order ----------------------------------------

    def test_mustard_channel_order_matches_pack(self):
        insight, survey, expected = MUSTARD
        result = handoff(survey, insight, RULES, _scenario_now(MUSTARD))
        self.assertEqual(
            [e["channel"] for e in result["channel_order"]],
            [e["channel"] for e in expected["channel_order"]],
        )

    def test_cow_channel_order_matches_pack(self):
        insight, survey, expected = COW
        result = handoff(survey, insight, RULES, _scenario_now(COW))
        self.assertEqual(
            [e["channel"] for e in result["channel_order"]],
            [e["channel"] for e in expected["channel_order"]],
        )

    def test_wheat_plan_channel_order_matches_pack(self):
        insight, survey, expected = WHEAT_PLAN
        result = handoff(survey, insight, RULES, _scenario_now(WHEAT_PLAN))
        self.assertEqual(
            [e["channel"] for e in result["channel_order"]],
            [e["channel"] for e in expected["channel_order"]],
        )

    def test_potato_channel_order_matches_pack(self):
        insight, survey, expected = POTATO
        result = handoff(survey, insight, RULES, _scenario_now(POTATO))
        self.assertEqual(
            [e["channel"] for e in result["channel_order"]],
            [e["channel"] for e in expected["channel_order"]],
        )


# ---------------------------------------------------------------------------
# 3. Fallback wait hours
# ---------------------------------------------------------------------------


class WaitHoursTests(unittest.TestCase):
    """Every channel except the last has wait_hours = rules value for that priority.
    The last channel always has wait_hours = None."""

    def _call(self, scenario: tuple) -> dict:
        insight, survey, _ = scenario
        return handoff(survey, insight, RULES, datetime.now(IST))

    def _wait_hours(self, result: dict) -> list[int | None]:
        return [entry["wait_hours"] for entry in result["channel_order"]]

    # last channel is always None ------------------------------------------

    def test_last_channel_has_no_wait_for_normal_priority(self):
        result = self._call(MUSTARD)
        self.assertIsNone(self._wait_hours(result)[-1])

    def test_last_channel_has_no_wait_for_high_priority(self):
        result = self._call(COW)
        self.assertIsNone(self._wait_hours(result)[-1])

    def test_last_channel_has_no_wait_for_low_priority(self):
        result = self._call(WHEAT_PLAN)
        self.assertIsNone(self._wait_hours(result)[-1])

    def test_single_channel_entry_has_no_wait(self):
        # Wheat irrigation: preferred=whatsapp, method=whatsapp → only one channel
        result = self._call(WHEAT_IRR)
        waits = self._wait_hours(result)
        self.assertEqual(len(waits), 1)
        self.assertIsNone(waits[0])

    # non-last channels carry the per-priority wait --------------------------

    def test_normal_priority_uses_24_hour_wait(self):
        expected_wait = RULES["wait_hours_by_priority"]["normal"]  # 24
        result = self._call(MUSTARD)  # app → whatsapp
        non_last = self._wait_hours(result)[:-1]
        self.assertTrue(all(w == expected_wait for w in non_last))

    def test_high_priority_uses_6_hour_wait(self):
        expected_wait = RULES["wait_hours_by_priority"]["high"]  # 6
        result = self._call(COW)  # voice → whatsapp
        non_last = self._wait_hours(result)[:-1]
        self.assertTrue(all(w == expected_wait for w in non_last))

    def test_low_priority_uses_48_hour_wait(self):
        expected_wait = RULES["wait_hours_by_priority"]["low"]  # 48
        result = self._call(WHEAT_PLAN)  # telegram → app → whatsapp
        non_last = self._wait_hours(result)[:-1]
        self.assertTrue(all(w == expected_wait for w in non_last))

    # worked-scenario wait hours match the pack ----------------------------

    def test_mustard_wait_hours_match_pack(self):
        insight, survey, expected = MUSTARD
        result = handoff(survey, insight, RULES, _scenario_now(MUSTARD))
        self.assertEqual(
            [e["wait_hours"] for e in result["channel_order"]],
            [e["wait_hours"] for e in expected["channel_order"]],
        )

    def test_cow_wait_hours_match_pack(self):
        insight, survey, expected = COW
        result = handoff(survey, insight, RULES, _scenario_now(COW))
        self.assertEqual(
            [e["wait_hours"] for e in result["channel_order"]],
            [e["wait_hours"] for e in expected["channel_order"]],
        )

    def test_wheat_plan_wait_hours_match_pack(self):
        insight, survey, expected = WHEAT_PLAN
        result = handoff(survey, insight, RULES, _scenario_now(WHEAT_PLAN))
        self.assertEqual(
            [e["wait_hours"] for e in result["channel_order"]],
            [e["wait_hours"] for e in expected["channel_order"]],
        )


# ---------------------------------------------------------------------------
# 4. Frequency caps
# ---------------------------------------------------------------------------


class FrequencyCapTests(unittest.TestCase):

    def _cap(self, scenario: tuple) -> dict:
        insight, survey, _ = scenario
        return handoff(survey, insight, RULES, datetime.now(IST))["frequency_cap"]

    def test_cap_key_contains_farmer_id(self):
        insight, survey, _ = MUSTARD
        cap = self._cap(MUSTARD)
        self.assertIn(insight["farmer_id"], cap["key"])

    def test_cap_key_has_correct_prefix(self):
        cap = self._cap(MUSTARD)
        # rules.json: "key": "mel:farmer:{farmer_id}"
        self.assertTrue(cap["key"].startswith("mel:farmer:"))

    def test_cap_key_format_matches_rules_template(self):
        insight, survey, _ = MUSTARD
        expected_key = RULES["frequency_cap"]["key"].format(farmer_id=insight["farmer_id"])
        cap = self._cap(MUSTARD)
        self.assertEqual(cap["key"], expected_key)

    def test_max_surveys_matches_rules(self):
        cap = self._cap(MUSTARD)
        self.assertEqual(cap["max_surveys"], RULES["frequency_cap"]["max_surveys"])  # 1

    def test_window_days_matches_rules(self):
        cap = self._cap(MUSTARD)
        self.assertEqual(cap["window_days"], RULES["frequency_cap"]["window_days"])  # 7

    # exempt flag ----------------------------------------------------------

    def test_normal_priority_is_not_exempt(self):
        cap = self._cap(MUSTARD)
        self.assertFalse(cap["exempt"])

    def test_low_priority_is_not_exempt(self):
        cap = self._cap(WHEAT_PLAN)
        self.assertFalse(cap["exempt"])

    def test_high_priority_is_exempt(self):
        cap = self._cap(COW)
        self.assertTrue(cap["exempt"])

    def test_high_priority_via_severity_is_also_exempt(self):
        insight, survey, _ = MUSTARD
        insight_high = {**insight, "severity": "high"}
        cap = handoff(survey, insight_high, RULES, datetime.now(IST))["frequency_cap"]
        self.assertTrue(cap["exempt"])

    def test_high_priority_via_emergency_intent_is_also_exempt(self):
        insight, survey, _ = MUSTARD
        insight_emergency = {**insight, "behavioral_intent": "emergency"}
        cap = handoff(survey, insight_emergency, RULES, datetime.now(IST))["frequency_cap"]
        self.assertTrue(cap["exempt"])

    # farmer-specific key differs across farmers ---------------------------

    def test_different_farmers_have_different_cap_keys(self):
        cap_mustard = self._cap(MUSTARD)
        cap_cow = self._cap(COW)
        self.assertNotEqual(cap_mustard["key"], cap_cow["key"])

    # worked-scenario cap values -------------------------------------------

    def test_mustard_cap_matches_pack(self):
        insight, survey, expected = MUSTARD
        result = handoff(survey, insight, RULES, _scenario_now(MUSTARD))
        self.assertEqual(result["frequency_cap"], expected["frequency_cap"])

    def test_cow_cap_matches_pack(self):
        insight, survey, expected = COW
        result = handoff(survey, insight, RULES, _scenario_now(COW))
        self.assertEqual(result["frequency_cap"], expected["frequency_cap"])

    def test_wheat_plan_cap_matches_pack(self):
        insight, survey, expected = WHEAT_PLAN
        result = handoff(survey, insight, RULES, _scenario_now(WHEAT_PLAN))
        self.assertEqual(result["frequency_cap"], expected["frequency_cap"])


# ---------------------------------------------------------------------------
# 5. Expiry calculation
# ---------------------------------------------------------------------------


class ExpiryTests(unittest.TestCase):
    """expires_at = send_window.not_after + expiry_days_after_window days."""

    _EXPIRY_DAYS = RULES["expiry_days_after_window"]  # 2

    def _expires_at(self, scenario: tuple) -> datetime:
        insight, survey, _ = scenario
        result = handoff(survey, insight, RULES, datetime.now(IST))
        return datetime.fromisoformat(result["expires_at"])

    def _not_after(self, scenario: tuple) -> datetime:
        _, survey, _ = scenario
        return datetime.fromisoformat(survey["send_window"]["not_after"])

    def test_expiry_is_not_after_plus_expiry_days(self):
        for scenario in (MUSTARD, WHEAT_IRR, POTATO, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                expected = self._not_after(scenario) + timedelta(days=self._EXPIRY_DAYS)
                actual = self._expires_at(scenario)
                self.assertEqual(actual, expected)

    def test_expiry_preserves_ist_offset(self):
        for scenario in (MUSTARD, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                expires = self._expires_at(scenario)
                # IST offset is +05:30 = 19800 seconds
                self.assertEqual(expires.utcoffset().total_seconds(), 19800)

    def test_expiry_time_matches_not_after_time_of_day(self):
        # expires_at inherits the same time-of-day as not_after (19:00 IST)
        for scenario in (MUSTARD, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                not_after = self._not_after(scenario)
                expires = self._expires_at(scenario)
                self.assertEqual(expires.hour, not_after.hour)
                self.assertEqual(expires.minute, not_after.minute)

    # worked-scenario expiry -----------------------------------------------

    def test_mustard_expiry_matches_pack(self):
        insight, survey, expected = MUSTARD
        result = handoff(survey, insight, RULES, _scenario_now(MUSTARD))
        self.assertEqual(result["expires_at"], expected["expires_at"])

    def test_cow_expiry_matches_pack(self):
        insight, survey, expected = COW
        result = handoff(survey, insight, RULES, _scenario_now(COW))
        self.assertEqual(result["expires_at"], expected["expires_at"])

    def test_wheat_plan_expiry_matches_pack(self):
        insight, survey, expected = WHEAT_PLAN
        result = handoff(survey, insight, RULES, _scenario_now(WHEAT_PLAN))
        self.assertEqual(result["expires_at"], expected["expires_at"])

    def test_potato_expiry_matches_pack(self):
        insight, survey, expected = POTATO
        result = handoff(survey, insight, RULES, _scenario_now(POTATO))
        self.assertEqual(result["expires_at"], expected["expires_at"])

    def test_wheat_irrigation_expiry_matches_pack(self):
        insight, survey, expected = WHEAT_IRR
        result = handoff(survey, insight, RULES, _scenario_now(WHEAT_IRR))
        self.assertEqual(result["expires_at"], expected["expires_at"])


# ---------------------------------------------------------------------------
# 6. Timestamps and identifiers
# ---------------------------------------------------------------------------


class TimestampAndIdTests(unittest.TestCase):

    # created_at -----------------------------------------------------------

    def test_created_at_is_normalized_to_ist(self):
        insight, survey, _ = MUSTARD
        now_utc = datetime(2026, 12, 4, 17, 30, 0, tzinfo=timezone.utc)  # 23:00 IST
        result = handoff(survey, insight, RULES, now_utc)
        created = datetime.fromisoformat(result["created_at"])
        # +05:30 offset (IST)
        self.assertEqual(created.utcoffset().total_seconds(), 19800)

    def test_created_at_has_no_microseconds(self):
        insight, survey, _ = MUSTARD
        now = datetime(2026, 12, 4, 23, 0, 0, 999999, tzinfo=IST)
        result = handoff(survey, insight, RULES, now)
        self.assertNotIn(".", result["created_at"].split("T")[1])

    def test_created_at_utc_input_converts_to_correct_ist_time(self):
        insight, survey, _ = MUSTARD
        # 2026-12-04T17:30:00Z == 2026-12-04T23:00:00+05:30
        now_utc = datetime(2026, 12, 4, 17, 30, 0, tzinfo=timezone.utc)
        result = handoff(survey, insight, RULES, now_utc)
        self.assertEqual(result["created_at"], "2026-12-04T23:00:00+05:30")

    def test_created_at_ist_input_is_preserved(self):
        insight, survey, _ = MUSTARD
        now_ist = datetime(2026, 12, 4, 23, 0, 0, tzinfo=IST)
        result = handoff(survey, insight, RULES, now_ist)
        self.assertEqual(result["created_at"], "2026-12-04T23:00:00+05:30")

    def test_created_at_matches_pack_when_now_matches_scenario(self):
        insight, survey, expected = MUSTARD
        result = handoff(survey, insight, RULES, _scenario_now(MUSTARD))
        self.assertEqual(result["created_at"], expected["created_at"])

    # handoff_id -----------------------------------------------------------

    def test_handoff_id_replaces_s_prefix_with_h(self):
        insight, survey, _ = MUSTARD
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertTrue(result["handoff_id"].startswith("H-"))
        self.assertFalse(result["handoff_id"].startswith("S-"))

    def test_handoff_id_preserves_numeric_suffix(self):
        insight, survey, _ = MUSTARD   # survey_id = "S-20931"
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(result["handoff_id"], "H-20931")

    def test_handoff_id_matches_pack(self):
        for scenario in (MUSTARD, WHEAT_IRR, POTATO, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                insight, survey, expected = scenario
                result = handoff(survey, insight, RULES, _scenario_now(scenario))
                self.assertEqual(result["handoff_id"], expected["handoff_id"])

    # farmerchat_flow ids --------------------------------------------------

    def test_campaign_id_is_deterministic(self):
        insight, survey, _ = MUSTARD
        r1 = handoff(survey, insight, RULES, datetime.now(IST))
        r2 = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(r1["farmerchat_flow"]["campaign_id"],
                         r2["farmerchat_flow"]["campaign_id"])

    def test_campaign_id_is_uuid5_of_campaign_name(self):
        insight, survey, _ = MUSTARD
        result = handoff(survey, insight, RULES, datetime.now(IST))
        expected = str(uuid5(NAMESPACE_URL, RULES["farmerchat_flow"]["campaign_name"]))
        self.assertEqual(result["farmerchat_flow"]["campaign_id"], expected)

    def test_flow_id_is_deterministic(self):
        insight, survey, _ = MUSTARD
        r1 = handoff(survey, insight, RULES, datetime.now(IST))
        r2 = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(r1["farmerchat_flow"]["flow_id"], r2["farmerchat_flow"]["flow_id"])

    def test_flow_id_differs_between_surveys(self):
        insight, survey, _ = MUSTARD
        r_mustard = handoff(survey, insight, RULES, datetime.now(IST))
        insight2, survey2, _ = COW
        r_cow = handoff(survey2, insight2, RULES, datetime.now(IST))
        self.assertNotEqual(r_mustard["farmerchat_flow"]["flow_id"],
                            r_cow["farmerchat_flow"]["flow_id"])

    # The pack's schedule_handoff.json files carry specific flow_id/campaign_id
    # example values, but schedule_handoff.schema.json constrains both only to
    # {"type": "string", "format": "uuid"} - a valid UUID, with no documented
    # derivation. The pack literals are v5 UUIDs from an undocumented namespace/name
    # that could not be reproduced. These tests therefore assert the real contract -
    # valid, stable, deterministic, per-survey-unique UUIDs - instead of byte-matching
    # arbitrary example IDs. Determinism, uniqueness and the generation formula are
    # pinned by test_flow_id_is_deterministic, test_flow_id_differs_between_surveys,
    # test_campaign_id_is_deterministic and test_campaign_id_is_uuid5_of_campaign_name.

    def test_flow_id_is_a_valid_uuid_for_every_scenario(self):
        for scenario in (MUSTARD, WHEAT_IRR, POTATO, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                insight, survey, _ = scenario
                result = handoff(survey, insight, RULES, _scenario_now(scenario))
                # UUID() raises if the value is not a well-formed UUID (the schema's
                # "format": "uuid" contract).
                self.assertEqual(str(UUID(result["farmerchat_flow"]["flow_id"])),
                                 result["farmerchat_flow"]["flow_id"])

    def test_campaign_id_is_a_valid_uuid_and_shared_across_scenarios(self):
        campaign_ids = set()
        for scenario in (MUSTARD, WHEAT_IRR, POTATO, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                insight, survey, _ = scenario
                result = handoff(survey, insight, RULES, _scenario_now(scenario))
                campaign_id = result["farmerchat_flow"]["campaign_id"]
                self.assertEqual(str(UUID(campaign_id)), campaign_id)
                campaign_ids.add(campaign_id)
        # One campaign_name -> one campaign_id across every survey, matching the pack,
        # which uses a single campaign_id for all worked scenarios.
        self.assertEqual(len(campaign_ids), 1)

    # flow_key -------------------------------------------------------------

    def test_flow_key_uses_lowercase_survey_id_with_underscores(self):
        insight, survey, _ = MUSTARD  # survey_id = "S-20931"
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(result["farmerchat_flow"]["flow_key"], "mel_s_20931")

    def test_flow_key_matches_pack(self):
        for scenario in (MUSTARD, WHEAT_IRR, POTATO, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                insight, survey, expected = scenario
                result = handoff(survey, insight, RULES, _scenario_now(scenario))
                self.assertEqual(result["farmerchat_flow"]["flow_key"],
                                 expected["farmerchat_flow"]["flow_key"])

    # send_hours pass-through ----------------------------------------------

    def test_send_hours_matches_rules(self):
        insight, survey, _ = MUSTARD
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(result["send_hours"], RULES["send_hours"])

    def test_send_hours_matches_pack(self):
        for scenario in (MUSTARD, COW, WHEAT_PLAN):
            with self.subTest(scenario=scenario[0]["insight_id"]):
                insight, survey, expected = scenario
                result = handoff(survey, insight, RULES, _scenario_now(scenario))
                self.assertEqual(result["send_hours"], expected["send_hours"])


# ---------------------------------------------------------------------------
# 7. Edge cases and timezone handling
# ---------------------------------------------------------------------------


class EdgeCaseTests(unittest.TestCase):
    """Behaviour on unusual or boundary inputs."""

    def _base_insight(self) -> dict:
        insight, _, _ = MUSTARD
        return dict(insight)

    def _base_survey(self) -> dict:
        _, survey, _ = MUSTARD
        return dict(survey)

    # timezone-aware datetimes ---------------------------------------------

    def test_now_with_ist_offset_string_parses_correctly(self):
        # handoff() receives a datetime; this confirms IST-aware input works
        insight = self._base_insight()
        survey = self._base_survey()
        now_ist = datetime.fromisoformat("2026-12-04T23:00:00+05:30")
        result = handoff(survey, insight, RULES, now_ist)
        self.assertEqual(result["created_at"], "2026-12-04T23:00:00+05:30")

    def test_now_with_utc_offset_is_converted_to_ist_in_output(self):
        insight = self._base_insight()
        survey = self._base_survey()
        # 2026-12-04T17:30:00+00:00 == 2026-12-04T23:00:00+05:30
        now_utc = datetime(2026, 12, 4, 17, 30, 0, tzinfo=timezone.utc)
        result = handoff(survey, insight, RULES, now_utc)
        created = datetime.fromisoformat(result["created_at"])
        self.assertEqual(created.utcoffset().total_seconds(), 19800)
        self.assertEqual(created.hour, 23)

    def test_microseconds_stripped_regardless_of_input(self):
        insight = self._base_insight()
        survey = self._base_survey()
        for microsecond in (0, 1, 500000, 999999):
            with self.subTest(microsecond=microsecond):
                now = datetime(2026, 12, 4, 23, 0, 0, microsecond, tzinfo=IST)
                result = handoff(survey, insight, RULES, now)
                created_str = result["created_at"]
                # ISO format: no fractional seconds means no "." in the time part
                self.assertNotIn(".", created_str.split("T")[1])

    # expires_at timezone --------------------------------------------------

    def test_expires_at_carries_ist_offset_from_not_after(self):
        insight = self._base_insight()
        survey = self._base_survey()
        result = handoff(survey, insight, RULES, datetime.now(IST))
        expires = datetime.fromisoformat(result["expires_at"])
        # not_after is "+05:30"; expires_at should inherit it
        self.assertEqual(expires.utcoffset().total_seconds(), 19800)

    # null channel in method mapping ---------------------------------------

    def test_null_method_channel_is_excluded_from_channel_order(self):
        # verification.method="none" maps to null in rules.json; null must be dropped
        insight = {**self._base_insight(),
                   "verification": {"needed": True, "timing": "two_to_five_days", "method": "none"}}
        survey = self._base_survey()
        result = handoff(survey, insight, RULES, datetime.now(IST))
        channels = [e["channel"] for e in result["channel_order"]]
        self.assertNotIn(None, channels)
        self.assertGreaterEqual(len(channels), 1)

    # structural completeness ----------------------------------------------

    def test_all_required_top_level_fields_present(self):
        insight = self._base_insight()
        survey = self._base_survey()
        result = handoff(survey, insight, RULES, datetime.now(IST))
        required = {
            "handoff_id", "created_at", "survey", "send_window", "send_hours",
            "channel_order", "frequency_cap", "priority", "expires_at", "farmerchat_flow",
        }
        self.assertEqual(required, required & result.keys())

    def test_farmerchat_flow_has_all_required_fields(self):
        insight = self._base_insight()
        survey = self._base_survey()
        flow = handoff(survey, insight, RULES, datetime.now(IST))["farmerchat_flow"]
        self.assertEqual({"org_id", "flow_key", "version", "campaign_id", "flow_id"}, set(flow))

    def test_frequency_cap_has_all_required_fields(self):
        insight = self._base_insight()
        survey = self._base_survey()
        cap = handoff(survey, insight, RULES, datetime.now(IST))["frequency_cap"]
        self.assertEqual({"key", "max_surveys", "window_days", "exempt"}, set(cap))

    def test_channel_order_entries_have_channel_and_wait_hours(self):
        insight = self._base_insight()
        survey = self._base_survey()
        for entry in handoff(survey, insight, RULES, datetime.now(IST))["channel_order"]:
            self.assertIn("channel", entry)
            self.assertIn("wait_hours", entry)

    def test_send_window_is_passed_through_unchanged(self):
        insight = self._base_insight()
        survey = self._base_survey()
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(result["send_window"], survey["send_window"])

    def test_survey_is_embedded_unchanged(self):
        insight = self._base_insight()
        survey = self._base_survey()
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(result["survey"], survey)

    # farmerchat_flow version and org_id -----------------------------------

    def test_flow_version_matches_rules(self):
        insight = self._base_insight()
        survey = self._base_survey()
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(result["farmerchat_flow"]["version"],
                         RULES["farmerchat_flow"]["version"])

    def test_flow_org_id_matches_rules(self):
        insight = self._base_insight()
        survey = self._base_survey()
        result = handoff(survey, insight, RULES, datetime.now(IST))
        self.assertEqual(result["farmerchat_flow"]["org_id"],
                         RULES["farmerchat_flow"]["org_id"])


if __name__ == "__main__":
    unittest.main()
