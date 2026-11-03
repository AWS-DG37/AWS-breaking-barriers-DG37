"""Offline evaluation, kept outside the generation implementation fingerprint."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mel.bundle import Bundle
from mel.policy import eligibility, send_window


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Assessed(_Evidence):
    kind: Literal["assessed"] = "assessed"
    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    @model_validator(mode="after")
    def valid_count(self) -> Assessed:
        if self.numerator > self.denominator:
            raise ValueError("numerator cannot exceed denominator")
        return self


class NotAssessed(_Evidence):
    kind: Literal["not_assessed"] = "not_assessed"
    reason: str


Measurement = Assessed | NotAssessed


class CaseComparison(_Evidence):
    golden_id: str
    rule: str
    expected_survey: bool
    predicted_survey: bool
    expected_reason: str | None
    predicted_reason: str | None
    reason_agreement: Measurement
    expected_window: dict[str, str] | None
    predicted_window: dict[str, str] | None
    window_agreement: Measurement


class LegacyReport(_Evidence):
    name: Literal["dg_agent_all_source"] = "dg_agent_all_source"
    source_cases: int
    source_sha256: str
    rules_sha256: str | None = None
    implementation_sha256: str | None = None
    eligibility: Measurement
    disagreement_ids: tuple[str, ...] = ()


class PolicyReport(_Evidence):
    mode: Literal["offline_policy"] = "offline_policy"
    split_version: str
    source_sha256: str
    holdout_sha256: str
    split_sha256: str
    scorer_sha256: str
    bundle_fingerprint: str
    source_cases: int
    excluded_cases: int
    eligibility: Measurement
    confusion: dict[str, int]
    reason_agreement: Measurement
    reason_coverage: Measurement
    window_agreement: Measurement
    per_rule: dict[str, Measurement]
    unresolved_reasons: dict[str, tuple[str, ...]]
    not_assessed: dict[str, NotAssessed]
    legacy: LegacyReport
    cases: tuple[CaseComparison, ...]
    notes: tuple[str, ...]


# Evaluation vocabulary adapter, not additional eligibility rules. None means
# no equivalence is defined; these cases still count in eligibility.
_REASON_EQUIVALENTS = {
    "junk_insight": "junk_insight",
    "nothing_to_verify": "generic_information",
    "waiting_for_details": "needs_more_context",
    "no_check_needed": "verification_not_needed",
    "window_after_season": "no_valid_season_window",
    "generic_information": "generic_information",
    "needs_more_context": "needs_more_context",
    "critical_needs_human_call": None,
    "crop_already_harvested": None,
}


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _measure(values: list[bool]) -> Measurement:
    if not values:
        return NotAssessed(reason="No applicable cases; no numeric score.")
    return Assessed(numerator=sum(values), denominator=len(values))


def _ids(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f"{label} must be a list of nonempty IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"duplicate {label} IDs")
    return value


def _rows(body: bytes, label: str) -> dict[str, dict]:
    result = {}
    for number, line in enumerate(body.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("golden_id"), str):
            raise ValueError(f"{label} row {number} needs a golden_id")
        key = row["golden_id"]
        if not key or key in result:
            raise ValueError(f"duplicate or empty {label} ID at row {number}")
        expected = row.get("expected")
        if (
            not isinstance(row.get("insight"), dict)
            or not isinstance(expected, dict)
            or type(expected.get("survey")) is not bool
            or not isinstance(expected.get("rule"), str)
        ):
            raise ValueError(f"{label} row {number} has invalid insight/expected fields")
        result[key] = row
    return result


def _load(golden_path: Path, split_path: Path) -> tuple[dict, dict, list[dict], bytes, bytes]:
    split_bytes = split_path.read_bytes()
    split = json.loads(split_bytes)
    if not isinstance(split, dict) or not isinstance(split.get("version"), str):
        raise ValueError("split needs a version")
    source = split.get("source")
    if not isinstance(source, str) or not source or "://" in source:
        raise ValueError("split source must be a local project-relative path")
    # The existing split's source is relative to the project containing eval/.
    root = split_path.resolve().parent.parent
    source_path = (root / source).resolve()
    if Path(source).is_absolute() or not source_path.is_relative_to(root):
        raise ValueError("split source must stay inside its project")
    source_bytes = source_path.read_bytes()
    if _sha(source_bytes) != split.get("source_sha256"):
        raise ValueError("source SHA-256 does not match the split manifest")
    source_rows = _rows(source_bytes, "source")
    examples = _ids(split.get("prompt_example_ids"), "prompt example")
    held = _ids(split.get("holdout_ids"), "holdout")
    exclusions = split.get("excluded")
    if not isinstance(exclusions, list) or any(not isinstance(x, dict) for x in exclusions):
        raise ValueError("split excluded must list excluded records")
    excluded = _ids([x.get("golden_id") for x in exclusions], "excluded")
    if (
        set(held) & set(excluded)
        or not set(examples) <= set(excluded)
        or set(held) | set(excluded) != set(source_rows)
    ):
        raise ValueError("holdout/excluded IDs must partition the source and exclude examples")
    example_insights = _ids(split.get("prompt_example_insight_ids"), "prompt insight")
    if set(example_insights) != {
        source_rows[key]["insight"].get("insight_id") for key in examples
    }:
        raise ValueError("prompt insight IDs do not match source examples")
    golden_bytes = golden_path.read_bytes()
    held_rows = _rows(golden_bytes, "holdout")
    if set(held_rows) != set(held):
        raise ValueError("holdout file must contain exactly the declared holdout IDs")
    if any(held_rows[key] != source_rows[key] for key in held):
        raise ValueError("holdout records differ from the hashed source")
    return split, source_rows, [held_rows[key] for key in held], golden_bytes, split_bytes


def _predict(bundle: Bundle, insight: dict) -> tuple[bool, str | None, dict | None]:
    # Match Select's boundary order, delegating all policy and schema decisions.
    if insight.get("quality_flag") == "junk":
        return False, "junk_insight", None
    errors = bundle.contracts.errors("insight", insight)
    if errors:
        return False, "invalid_insight: " + "; ".join(errors), None
    reason = eligibility(insight, bundle.snapshot["rules"])
    if reason:
        return False, reason, None
    try:
        window = send_window(insight, bundle.snapshot["rules"])
    except ValueError as error:
        return False, str(error), None
    return True, None, window


def _expected_window(expected: dict) -> dict[str, str] | None:
    value = expected.get("send_window")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("expected send_window must contain start/end timestamps")
    try:
        start, end = (datetime.fromisoformat(value[k]) for k in ("start", "end"))
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("expected window must be ordered, timezone-aware timestamps") from error
    return {"not_before": value["start"], "not_after": value["end"]}


def _legacy(source_rows: dict[str, dict], source_hash: str) -> LegacyReport:
    try:
        import dg_agent.checks as reference
        import dg_agent.models as models
    except ModuleNotFoundError as error:
        if error.name not in {"dg_agent", "dg_agent.checks", "dg_agent.models"}:
            raise
        return LegacyReport(
            source_cases=len(source_rows), source_sha256=source_hash,
            eligibility=NotAssessed(reason="Legacy reference package is not installed."),
        )
    try:
        reference_rules = reference.rules()
    except FileNotFoundError:
        return LegacyReport(
            source_cases=len(source_rows), source_sha256=source_hash,
            eligibility=NotAssessed(reason="Legacy reference pack assets are unavailable."),
        )
    values, misses = [], []
    for key, row in source_rows.items():
        matches = reference.decide(row["insight"])[0] == row["expected"]["survey"]
        values.append(matches)
        if not matches:
            misses.append(key)
    return LegacyReport(
        source_cases=len(source_rows), source_sha256=source_hash,
        rules_sha256=_sha(json.dumps(reference_rules, sort_keys=True).encode()),
        implementation_sha256=_sha(
            Path(reference.__file__).read_bytes() + Path(models.__file__).read_bytes()
        ),
        eligibility=_measure(values), disagreement_ids=tuple(misses),
    )


def score_policy(*, bundle: Bundle, golden_path: Path, split_path: Path) -> PolicyReport:
    """Score the declared holdout without generation, storage, or network access.

    Labels are evaluation-only. Every holdout row counts in eligibility; reason
    equivalence and exact window agreement have separate, explicit denominators.
    The legacy comparator always uses the complete hashed source, not the holdout.
    """
    split, source, rows, golden_bytes, split_bytes = _load(golden_path, split_path)
    allowed_reasons = set(bundle.snapshot["rules"]["survey_by_resolution"]) | {
        "junk_insight", "invalid_insight", "verification_not_needed", "no_valid_season_window",
    }
    if any(v is not None and v not in allowed_reasons for v in _REASON_EQUIVALENTS.values()):
        raise ValueError("reason equivalence points outside the production reason vocabulary")
    comparisons, decisions, reasons, windows, coverage = [], [], [], [], []
    per_rule = defaultdict(list)
    unresolved = defaultdict(list)
    confusion = dict.fromkeys(
        ("true_positive", "true_negative", "false_positive", "false_negative"), 0,
    )
    for row in rows:
        expected = row["expected"]
        wants = expected["survey"]
        predicted, reason, window = _predict(bundle, row["insight"])
        matches = wants == predicted
        decisions.append(matches)
        per_rule[expected["rule"]].append(matches)
        key = ("true" if matches else "false") + ("_positive" if predicted else "_negative")
        confusion[key] += 1
        expected_reason = expected.get("reason_code")
        reason_result: Measurement = NotAssessed(reason="No no-survey reason is expected.")
        if not wants:
            if not isinstance(expected_reason, str) or expected_reason not in _REASON_EQUIVALENTS:
                raise ValueError(f"unknown golden reason code for {row['golden_id']}")
            equivalent = _REASON_EQUIVALENTS[expected_reason]
            coverage.append(equivalent is not None)
            if equivalent is None:
                unresolved[expected_reason].append(row["golden_id"])
                reason_result = NotAssessed(reason="Reason equivalence/policy is unresolved.")
            else:
                agrees = equivalent == (reason.split(":", 1)[0] if reason else None)
                reasons.append(agrees)
                reason_result = _measure([agrees])
        expected_window = _expected_window(expected)
        window_result: Measurement = NotAssessed(reason="No golden window is specified.")
        if expected_window is not None:
            agrees = window is not None and all(
                datetime.fromisoformat(window[key]) == datetime.fromisoformat(value)
                for key, value in expected_window.items()
            )
            windows.append(agrees)
            window_result = _measure([agrees])
        comparisons.append(CaseComparison(
            golden_id=row["golden_id"], rule=expected["rule"],
            expected_survey=wants, predicted_survey=predicted,
            expected_reason=expected_reason, predicted_reason=reason,
            reason_agreement=reason_result, expected_window=expected_window,
            predicted_window=window, window_agreement=window_result,
        ))
    source_hash = split["source_sha256"]
    package = Path(__file__).parent
    scorer_bytes = b"".join(
        str(path.relative_to(package)).encode() + path.read_bytes()
        for path in sorted(package.rglob("*.py"))
    )
    return PolicyReport(
        split_version=split["version"], source_sha256=source_hash,
        holdout_sha256=_sha(golden_bytes), split_sha256=_sha(split_bytes),
        scorer_sha256=_sha(scorer_bytes),
        bundle_fingerprint=bundle.fingerprint(golden_bytes, "offline-policy"),
        source_cases=len(source), excluded_cases=len(split["excluded"]),
        eligibility=_measure(decisions), confusion=confusion,
        reason_agreement=_measure(reasons), reason_coverage=_measure(coverage),
        window_agreement=_measure(windows),
        per_rule={key: _measure(value) for key, value in sorted(per_rule.items())},
        unresolved_reasons={key: tuple(value) for key, value in sorted(unresolved.items())},
        not_assessed={key: NotAssessed(reason=reason) for key, reason in {
            "relevance_grounding": "Requires generated surveys and an independent MEL rubric.",
            "language_quality": "Requires review of generated farmer-facing language.",
            "follow_up_semantics": "No generated survey or simulated responses are assessed.",
            "channel_precedence": "Golden channel precedence is unresolved; no channel score.",
            "generation_reliability": "Offline policy scoring makes no generation calls.",
        }.items()},
        legacy=_legacy(source, source_hash), cases=tuple(comparisons),
        notes=(
            "Eligibility includes every holdout case, including unresolved policy misses.",
            "Reason agreement uses explicit equivalents; reason_coverage shows coverage.",
            "Exact window agreement is descriptive; golden/rule precedence is unresolved.",
            "The candidate split is not a guarantee against semantic prompt leakage.",
        ),
    )
