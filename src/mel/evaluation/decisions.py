"""Score deterministic survey decisions and send windows against the golden set.

No model calls: this runs in a unit test and on every rule change.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from mel.decision import Decision
from mel.evaluation.golden import GoldenCase

BASELINE_LABEL = (
    "regression_baseline: the rules were written while reading all 150 golden rows"
)


class Decider(Protocol):
    def decide(self, insight: Mapping) -> Decision: ...


@dataclass
class DecisionReport:
    cases: int = 0
    decision_correct: int = 0
    rule_correct: int = 0
    confusion: Counter = field(default_factory=Counter)
    rule_cases: Counter = field(default_factory=Counter)
    rule_hits: Counter = field(default_factory=Counter)
    windows_compared: int = 0
    window_start_date_match: int = 0
    window_end_date_match: int = 0
    window_exact_match: int = 0
    mismatches: list[dict] = field(default_factory=list)
    window_mismatches: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        def rate(hits: int, total: int) -> float | None:
            return round(hits / total, 4) if total else None

        return {
            "label": BASELINE_LABEL,
            "cases": self.cases,
            "decision_accuracy": rate(self.decision_correct, self.cases),
            "decision_correct": self.decision_correct,
            "rule_accuracy": rate(self.rule_correct, self.cases),
            "rule_correct": self.rule_correct,
            "confusion": {
                key: self.confusion[key]
                for key in ("true_survey", "true_no_survey", "false_survey", "false_no_survey")
            },
            "by_expected_rule": {
                rule: {"cases": total, "correct": self.rule_hits[rule]}
                for rule, total in sorted(self.rule_cases.items())
            },
            "window": {
                "compared": self.windows_compared,
                "start_date_match": rate(self.window_start_date_match, self.windows_compared),
                "start_date_matches": self.window_start_date_match,
                "end_date_match": rate(self.window_end_date_match, self.windows_compared),
                "end_date_matches": self.window_end_date_match,
                "exact_match": rate(self.window_exact_match, self.windows_compared),
                "exact_matches": self.window_exact_match,
            },
            "mismatches": self.mismatches,
            "window_mismatches": self.window_mismatches,
        }


def _confusion_key(expected: bool, got: bool) -> str:
    if expected:
        return "true_survey" if got else "false_no_survey"
    return "false_survey" if got else "true_no_survey"


def _date(value: str) -> str:
    return datetime.fromisoformat(value).date().isoformat()


def score_decisions(decider: Decider, cases: list[GoldenCase]) -> DecisionReport:
    report = DecisionReport()
    for case in cases:
        decision = decider.decide(case.insight)
        expected_rule = case.expected["rule"]
        report.cases += 1
        report.confusion[_confusion_key(case.expects_survey, decision.survey)] += 1
        report.rule_cases[expected_rule] += 1
        if decision.survey == case.expects_survey:
            report.decision_correct += 1
        if decision.rule == expected_rule:
            report.rule_correct += 1
            report.rule_hits[expected_rule] += 1
        else:
            report.mismatches.append(
                {
                    "golden_id": case.golden_id,
                    "expected_rule": expected_rule,
                    "expected_reason": case.expected.get("reason_code"),
                    "rule": decision.rule,
                    "reason": decision.reason_code,
                }
            )
        if case.expects_survey and decision.survey:
            _score_window(report, case, decision)
    return report


def _score_window(report: DecisionReport, case: GoldenCase, decision: Decision) -> None:
    expected = case.expected["send_window"]
    got = decision.window.to_contract()
    start = _date(expected["start"]) == _date(got["not_before"])
    end = _date(expected["end"]) == _date(got["not_after"])
    exact = (
        datetime.fromisoformat(expected["start"]) == decision.window.not_before
        and datetime.fromisoformat(expected["end"]) == decision.window.not_after
    )
    report.windows_compared += 1
    report.window_start_date_match += start
    report.window_end_date_match += end
    report.window_exact_match += exact
    if not (start and end):
        report.window_mismatches.append(
            {
                "golden_id": case.golden_id,
                "timing": case.insight["verification"]["timing"],
                "action_window_days": case.insight["action_window_days"],
                "expected": expected,
                "got": got,
            }
        )
