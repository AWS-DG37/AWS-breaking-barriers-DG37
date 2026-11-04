"""Survey-or-not rules, one class per golden rule code.

Each rule either decides (returns a Decision) or passes (returns None). The engine
applies them in order; the first decision wins. Rules never call a model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from typing import Protocol

from mel.clock import IST
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
    """R5: the insight extractor marked the advice as needing no check."""

    code: str = "R5"

    def evaluate(self, case: Case) -> Decision | None:
        if case.insight["verification"]["needed"]:
            return None
        return Decision(
            self.code,
            survey=False,
            reason_code="no_check_needed",
            explanation="Verification not needed",
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
        advice_day = datetime.fromisoformat(case.insight["created_at"]).astimezone(IST).date()
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
