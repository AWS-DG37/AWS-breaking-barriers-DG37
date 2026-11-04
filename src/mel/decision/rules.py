"""Survey-or-not rules, one class per golden rule code.

Each rule either decides (returns a Decision) or passes (returns None). The engine
applies them in order; the first decision wins. Rules never call a model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Protocol

from mel.clock import parse_ist
from mel.decision.calendar import CropCalendar
from mel.decision.model import Decision, SendWindow
from mel.decision.windows import WindowPolicy


@dataclass
class Case:
    """One insight under evaluation. The window is computed at most once, and only
    by rules that need it, so broken rows never reach the window policy."""

    insight: Mapping
    schema_errors: list[str]
    window_policy: WindowPolicy = field(repr=False)

    @cached_property
    def window(self) -> SendWindow | None:
        return self.window_policy.window_for(self.insight)


class Rule(Protocol):
    code: str

    def evaluate(self, case: Case) -> Decision | None: ...


@dataclass(frozen=True)
class JunkInsight:
    """R1: rows marked junk, or rows that break the insight schema."""

    code: str = "R1"

    def evaluate(self, case: Case) -> Decision | None:
        if case.insight.get("quality_flag") != "junk" and not case.schema_errors:
            return None
        return Decision(
            self.code,
            survey=False,
            reason_code="junk_insight",
            explanation="; ".join(case.schema_errors) or "Marked junk",
        )


@dataclass(frozen=True)
class NoSurveyResolution:
    """R3/R4: resolution types for which the pack's rules say no survey."""

    code: str
    resolution_type: str
    reason_code: str
    explanation: str

    def evaluate(self, case: Case) -> Decision | None:
        if case.insight["resolution_type"] != self.resolution_type:
            return None
        return Decision(
            self.code,
            survey=False,
            reason_code=self.reason_code,
            explanation=self.explanation,
            no_survey_code=self.resolution_type,
        )


@dataclass(frozen=True)
class VerificationNotNeeded:
    """R5: the insight extractor marked the advice as needing no check.

    Fires only when `needed` is explicitly False. A missing or non-boolean `needed`
    is malformed metadata, not a clean "no check needed", so it falls through to R5b
    rather than being quietly treated as needing no survey.
    """

    code: str = "R5"

    def evaluate(self, case: Case) -> Decision | None:
        if case.insight.get("verification", {}).get("needed") is not False:
            return None
        return Decision(
            self.code,
            survey=False,
            reason_code="no_check_needed",
            explanation="Verification not needed",
        )


@dataclass(frozen=True)
class InvalidVerification:
    """R5b: verification metadata that is malformed or self-contradictory.

    Reaching this rule means R5 did not fire, so `needed` is not explicitly False.
    A survey therefore needs a real check, which needs a usable `timing`. This rule
    declines when the metadata cannot support one, keeping "no check needed" (R5)
    distinct from "the check cannot be scheduled because the metadata is wrong":

      * `needed` is missing or not a boolean (schema requires a boolean);
      * `timing` is missing or null;
      * `timing` is "none" while `needed` is True (a check is required but no timing
        is given - a contradiction);
      * `timing` is not one of the windowed timings the rules define.

    The valid timings come from the rules (`send_window_days`), so adding a timing to
    the pack does not require editing this rule.
    """

    valid_timings: Collection[str]
    code: str = "R5b"

    def evaluate(self, case: Case) -> Decision | None:
        verification = case.insight.get("verification")
        if not isinstance(verification, Mapping):
            return self._decline("verification metadata is missing")
        if not isinstance(verification.get("needed"), bool):
            return self._decline("verification.needed must be true or false")
        timing = verification.get("timing")
        if timing is None:
            return self._decline("verification.timing is missing")
        if timing == "none":
            return self._decline("verification.needed is true but timing is 'none'")
        if timing not in self.valid_timings:
            return self._decline(f"verification.timing {timing!r} has no send window")
        return None

    def _decline(self, explanation: str) -> Decision:
        return Decision(
            self.code,
            survey=False,
            reason_code="invalid_verification_timing",
            explanation=explanation,
        )


@dataclass(frozen=True)
class WindowAfterSeason:
    """R6: no valid send window fits before the season ends."""

    code: str = "R6"

    def evaluate(self, case: Case) -> Decision | None:
        if case.window is not None:
            return None
        return Decision(
            self.code,
            survey=False,
            reason_code="window_after_season",
            explanation="No valid send window before the season ends",
        )


@dataclass(frozen=True)
class CropAlreadyHarvested:
    """R7: the crop's calendar had ended by the day of the advice; nothing is in the field."""

    calendar: CropCalendar
    code: str = "R7"

    def evaluate(self, case: Case) -> Decision | None:
        last_day = self.calendar.last_day(case.insight["asset"]["name"])
        advice_day = parse_ist(case.insight["created_at"]).date()
        if last_day is None or advice_day < last_day:
            return None
        return Decision(
            self.code,
            survey=False,
            reason_code="crop_already_harvested",
            explanation=f"Crop calendar ends {last_day.isoformat()}, by the day of the advice",
        )


class DecisionEngine:
    """Applies rules in order. If none declines, the insight gets a survey (R8)."""

    survey_rule = "R8"

    def __init__(
        self,
        rules: list[Rule],
        window_policy: WindowPolicy,
        schema_errors: Callable[[Mapping], list[str]],
    ):
        self._rules = rules
        self._window_policy = window_policy
        self._schema_errors = schema_errors

    def decide(self, insight: Mapping) -> Decision:
        case = Case(insight, self._schema_errors(insight), self._window_policy)
        for rule in self._rules:
            decision = rule.evaluate(case)
            if decision is not None:
                return decision
        if case.window is None:
            raise ValueError("Rule list must decline insights without a send window (R6)")
        return Decision(self.survey_rule, survey=True, window=case.window)
