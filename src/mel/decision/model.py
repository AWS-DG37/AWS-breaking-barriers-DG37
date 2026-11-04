"""Value objects produced by the decision engine. No I/O and no model calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SendWindow:
    not_before: datetime
    not_after: datetime

    def to_contract(self) -> dict:
        return {"not_before": self.not_before.isoformat(), "not_after": self.not_after.isoformat()}


@dataclass(frozen=True)
class Decision:
    """Survey or not, which golden rule decided it, and why.

    `rule` and `reason_code` use the golden set's vocabulary (R1-R8). `no_survey_code`
    is set only for the reasons the pack's no_survey contract can represent.
    """

    rule: str
    survey: bool
    reason_code: str | None = None
    explanation: str | None = None
    no_survey_code: str | None = None
    window: SendWindow | None = None
