"""Deterministic survey checks that run before the paid review call.

Ported from the hackathon starter kit's check_survey.py (add-mel-starter). The draft
models and the pack schema already enforce shape: label length, option counts and
follow_up values. These cover what they cannot: emoji, exact dates, personal-
information words, script that does not match the language, and surveys whose
answers cannot route a bad outcome back for follow-up.
"""

from __future__ import annotations

import re

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
DATE = re.compile(
    r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b",
    re.I,
)
# ponytail: a keyword list, not a classifier. The model reviewer still judges meaning;
# upgrade to a reviewed term list per language if false positives appear.
PERSONAL_EN = re.compile(
    r"\b(name|phone|mobile|address|aadhaa?r|income|salary|bank|account number)\b", re.I
)
# Hindi terms match whole words only: as substrings, आधार (Aadhaar) hits आधारित ("based
# on"). पता (address) is left out: पता नहीं is the everyday "don't know" answer.
PERSONAL_HI = frozenset({"नाम", "फोन", "मोबाइल", "आधार", "आमदनी", "कमाई", "बैंक"})
HINDI_WORD = re.compile(r"[ऀ-ॣ०-ॿ]+")  # Excludes the danda punctuation.


def _personal(text: str) -> bool:
    return bool(PERSONAL_EN.search(text)) or not PERSONAL_HI.isdisjoint(HINDI_WORD.findall(text))


def survey_issues(survey: dict) -> list[str]:
    """Blocking defects code can find. Empty means none were found, not that it is good."""
    issues = []
    language = survey.get("language")
    items = survey.get("items") or []
    for item in items:
        key = item.get("item_id", "?")
        texts = [("prompt", item.get("prompt") or "")] + [
            ("label", o.get("label") or "") for o in item.get("options") or []
        ]
        for kind, text in texts:
            if EMOJI.search(text):
                issues.append(f"{key}: remove the emoji from the {kind}")
            if DATE.search(text):
                issues.append(f"{key}: the {kind} names an exact date; use relative timing")
            if _personal(text):
                issues.append(f"{key}: the {kind} may ask for personal information: {text[:40]}")
            if language == "en-IN" and DEVANAGARI.search(text):
                issues.append(f"{key}: the {kind} uses Devanagari in an English survey")
        if language == "hi-IN" and not DEVANAGARI.search(item.get("prompt") or ""):
            issues.append(f"{key}: the Hindi prompt has no Devanagari text")
    routable = any(o.get("follow_up") for item in items for o in item.get("options") or []) or any(
        item.get("follow_up_reason") for item in items
    )
    if items and not routable:
        issues.append(
            "No answer carries a follow_up reason, so a bad outcome cannot be routed back"
        )
    return issues
