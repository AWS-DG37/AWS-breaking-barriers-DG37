"""Digital Green's golden set: insights with the expected survey decision and survey."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoldenCase:
    golden_id: str
    insight: dict
    expected: dict

    @property
    def expects_survey(self) -> bool:
        return self.expected["survey"]


def load_golden(path: Path) -> list[GoldenCase]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            cases.append(GoldenCase(row["golden_id"], row["insight"], row["expected"]))
    return cases


def insights_jsonl(cases: list[GoldenCase]) -> str:
    """Worker input for these cases. Expected answers never leave this module."""
    return "".join(json.dumps(case.insight, ensure_ascii=False) + "\n" for case in cases)
