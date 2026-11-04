"""Deterministic survey decisions: survey or not (golden rules R1-R8) and the send window.

`build_engine` is the composition root; everything else depends on the abstractions.
"""

from collections.abc import Callable, Mapping
from datetime import date

from mel.config import DecisionPolicy
from mel.decision.calendar import CropCalendar
from mel.decision.model import Decision, SendWindow
from mel.decision.rules import (
    CropAlreadyHarvested,
    DecisionEngine,
    JunkInsight,
    NeedsHumanCall,
    NoSurveyResolution,
    VerificationNotNeeded,
    WindowAfterSeason,
)
from mel.decision.windows import GoldenWindowPolicy, PackWindowPolicy, WindowPolicy

__all__ = [
    "CropCalendar",
    "Decision",
    "DecisionEngine",
    "GoldenWindowPolicy",
    "PackWindowPolicy",
    "SendWindow",
    "WindowPolicy",
    "build_engine",
]


def build_engine(
    rules: Mapping,
    policy: DecisionPolicy,
    calendar: CropCalendar,
    schema_errors: Callable[[Mapping], list[str]],
) -> DecisionEngine:
    by_resolution = rules["survey_by_resolution"]
    return DecisionEngine(
        [
            JunkInsight(),
            NeedsHumanCall(frozenset(policy.human_call_severities)),
            NoSurveyResolution(
                "R3",
                "generic_information",
                "nothing_to_verify",
                by_resolution["generic_information"]["asks"],
            ),
            NoSurveyResolution(
                "R4",
                "needs_more_context",
                "waiting_for_details",
                by_resolution["needs_more_context"]["asks"],
            ),
            VerificationNotNeeded(),
            WindowAfterSeason(),
            CropAlreadyHarvested(calendar),
        ],
        window_policy(rules, policy),
        schema_errors,
    )


def window_policy(rules: Mapping, policy: DecisionPolicy) -> WindowPolicy:
    if policy.window_policy == "golden_v1":
        season_end = date.fromisoformat(rules["window_rules"]["season_end"])
        return GoldenWindowPolicy(policy.golden_v1, season_end)
    return PackWindowPolicy(rules["send_window_days"], rules["window_rules"])
