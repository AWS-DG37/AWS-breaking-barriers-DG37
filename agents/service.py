"""One entry point for the MEL survey pipeline, shared by the sweep, the AgentCore runtime
(main.py) and the MEL console's Agents view.

generate_survey() validates the request (models must be on the allowlist, the insight must have
the fields the pipeline reads) and runs sweep.run_pipeline, which is the same code the model
comparison scores. The result dict is the same shape as one entry of a run file's results[].
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from sweep import DEFAULT_TRANSLATOR, DEFAULT_VERIFIER, run_pipeline  # noqa: E402

DEFAULT_GENERATOR = os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-5-5")

# Models the deployed agent may call. The runtime's IAM role is scoped to the same list.
GENERATORS = (
    "global.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-opus-5-5",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.openai.gpt-5.5",
)
VERIFIERS = ("global.anthropic.claude-sonnet-5-5", "global.anthropic.claude-haiku-5-5")
TRANSLATORS = (DEFAULT_TRANSLATOR,)
PIPELINES = ("direct", "translate")
# Effort levels the console may ask for on the verifier. None leaves the model's default.
VERIFIER_EFFORTS = ("low", "medium", "high")
MAX_RETRIES = 2

REQUIRED_FIELDS = (
    "insight_id",
    "conversation_language",
    "asset",
    "resolution_type",
    "advice_summary",
    "query_summary",
)


class BadRequest(ValueError):
    """The request cannot run as given. The message is safe to show to the caller."""


def options():
    return {
        "generators": list(GENERATORS),
        "verifiers": list(VERIFIERS),
        "translators": list(TRANSLATORS),
        "pipelines": list(PIPELINES),
        "verifier_efforts": list(VERIFIER_EFFORTS),
        "defaults": {
            "generator": DEFAULT_GENERATOR,
            "verifier": DEFAULT_VERIFIER,
            "translator": DEFAULT_TRANSLATOR,
            "pipeline": "direct",
            "max_retries": 1,
        },
    }


def _pick(value, allowed, default, label):
    value = value or default
    if value not in allowed:
        raise BadRequest(f"{label} '{value}' is not allowed. Choose one of: {', '.join(allowed)}")
    return value


def _check_insight(insight):
    if not isinstance(insight, dict):
        raise BadRequest("insight must be a JSON object")
    missing = [f for f in REQUIRED_FIELDS if f not in insight]
    if missing:
        raise BadRequest(f"insight is missing fields: {', '.join(missing)}")
    if not isinstance(insight.get("asset"), dict):
        raise BadRequest("insight.asset must be an object")


def decide_only(insight):
    """The survey / no-survey rules on their own. Code only, so it answers at once."""
    from decide import decide  # path set up by run_one, imported through sweep

    _check_insight(insight)
    try:
        return decide(insight)
    except (KeyError, TypeError) as error:
        raise BadRequest(f"insight is missing data the rules need ({error})") from None


def generate_survey(
    insight,
    generator=None,
    verifier=None,
    pipeline="direct",
    translator=None,
    max_retries=1,
    verifier_effort=None,
):
    """Run one insight through the pipeline and return the result dict."""
    _check_insight(insight)
    generator = _pick(generator, GENERATORS, DEFAULT_GENERATOR, "generator")
    verifier = _pick(verifier, VERIFIERS, DEFAULT_VERIFIER, "verifier")
    translator = _pick(translator, TRANSLATORS, DEFAULT_TRANSLATOR, "translator")
    pipeline = _pick(pipeline, PIPELINES, "direct", "pipeline")
    try:
        max_retries = int(max_retries)
    except (TypeError, ValueError):
        raise BadRequest("max_retries must be a whole number") from None
    if not 0 <= max_retries <= MAX_RETRIES:
        raise BadRequest(f"max_retries must be between 0 and {MAX_RETRIES}")
    if verifier_effort is not None and verifier_effort not in VERIFIER_EFFORTS:
        raise BadRequest(
            f"verifier_effort '{verifier_effort}' is not allowed. "
            f"Choose one of: {', '.join(VERIFIER_EFFORTS)}"
        )
    result = run_pipeline(
        insight,
        generator,
        verifier,
        pipeline,
        translator,
        max_retries,
        verifier_effort=verifier_effort,
    )
    result["models"] = {
        "generator": generator,
        "verifier": verifier,
        "verifier_effort": verifier_effort,
        "translator": translator if pipeline == "translate" else None,
    }
    return result
