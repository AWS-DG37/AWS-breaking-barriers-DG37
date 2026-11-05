"""Classifier gate: a Strands agent makes the survey or no survey call instead of decide.py.

The model answers {"rule": "R1"|"R3"|"R4"|"R5"|"R8", "reason": "..."}. parse() turns that into the
same decision dict decide() returns, using decide.REASON_CODES, so both gates feed the pipeline
the same shape. Anything else is refused with ValueError, and the pipeline records a failure.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULES = ROOT / "skills" / "mel-survey-rules"
sys.path.insert(0, str(RULES / "scripts"))
from decide import REASON_CODES, decision  # noqa: E402

PROMPT = RULES / "references" / "classifier_prompt.md"


def prompt_file():
    """classifier_prompt.md, or its variant in MEL_PROMPT_DIR when that folder has one."""
    override = Path(os.environ["MEL_PROMPT_DIR"]) / PROMPT.name if os.environ.get("MEL_PROMPT_DIR") else None
    return override if override and override.is_file() else PROMPT


def load_prompts():
    """Return (system, user_template)."""
    blocks = re.findall(r"```\n(.*?)```", prompt_file().read_text(encoding="utf-8"), flags=re.S)
    return blocks[0], blocks[1]


def build_user(tpl, insight):
    # Junk insights are exactly the ones with missing fields, so every field may be absent.
    i = insight
    return tpl.format(
        decision_episode_type=i.get("decision_episode_type"), concern=i.get("concern"),
        resolution_type=i.get("resolution_type"),
        verification_needed=(i.get("verification") or {}).get("needed"),
        query_summary=i.get("query_summary"), advice_summary=i.get("advice_summary"))


def parse(raw):
    """Model text -> decision dict with the classifier's reason. Raises ValueError."""
    s = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    start, end = s.find("{"), s.rfind("}")
    try:
        answer = json.loads(s[start:end + 1] if start != -1 and end > start else s)
    except json.JSONDecodeError as e:
        raise ValueError(f"classifier output was not JSON: {e}") from None
    rule = answer.get("rule") if isinstance(answer, dict) else None
    if rule not in REASON_CODES:
        raise ValueError(f"classifier returned rule {rule!r}; expected one of {', '.join(REASON_CODES)}")
    return {**decision(rule), "reason": str(answer.get("reason") or "")}
