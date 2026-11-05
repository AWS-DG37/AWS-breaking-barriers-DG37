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
from sweep import (  # noqa: E402
    DEFAULT_CLASSIFIER,
    DEFAULT_TRANSLATOR,
    DEFAULT_VERIFIER,
    GATES,
    gate_decision,
    run_pipeline,
)

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
CLASSIFIERS = (DEFAULT_CLASSIFIER, "global.anthropic.claude-sonnet-5-5")
PIPELINES = ("direct", "translate")
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


class DecisionFailed(RuntimeError):
    """The classifier gave no usable answer. The message is safe to show to the caller."""


def options():
    return {
        "generators": list(GENERATORS),
        "verifiers": list(VERIFIERS),
        "translators": list(TRANSLATORS),
        "classifiers": list(CLASSIFIERS),
        "pipelines": list(PIPELINES),
        "gates": list(GATES),
        "defaults": {
            "generator": DEFAULT_GENERATOR,
            "verifier": DEFAULT_VERIFIER,
            "translator": DEFAULT_TRANSLATOR,
            "classifier": DEFAULT_CLASSIFIER,
            "pipeline": "direct",
            "gate": "rules",
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


def decide_only(insight, gate="rules", classifier=None):
    """Survey or no survey on its own. The rules answer at once; the classifier in a few seconds."""
    _check_insight(insight)
    gate = _pick(gate, GATES, "rules", "gate")
    classifier = _pick(classifier, CLASSIFIERS, DEFAULT_CLASSIFIER, "classifier")
    try:
        decision = gate_decision(insight, gate, classifier)
    except (KeyError, TypeError) as error:
        raise BadRequest(f"insight is missing data the rules need ({error})") from None
    except Exception as error:  # an unusable answer or a model call that failed
        raise DecisionFailed(f"The classifier could not decide ({type(error).__name__})") from None
    return {
        **decision,
        "gate": gate,
        "classifier": classifier if gate == "classifier" else None,
    }


def generate_survey(
    insight, generator=None, verifier=None, pipeline="direct", translator=None, max_retries=1,
    gate="rules", classifier=None,
):
    """Run one insight through the pipeline and return the result dict."""
    _check_insight(insight)
    generator = _pick(generator, GENERATORS, DEFAULT_GENERATOR, "generator")
    verifier = _pick(verifier, VERIFIERS, DEFAULT_VERIFIER, "verifier")
    translator = _pick(translator, TRANSLATORS, DEFAULT_TRANSLATOR, "translator")
    classifier = _pick(classifier, CLASSIFIERS, DEFAULT_CLASSIFIER, "classifier")
    pipeline = _pick(pipeline, PIPELINES, "direct", "pipeline")
    gate = _pick(gate, GATES, "rules", "gate")
    try:
        max_retries = int(max_retries)
    except (TypeError, ValueError):
        raise BadRequest("max_retries must be a whole number") from None
    if not 0 <= max_retries <= MAX_RETRIES:
        raise BadRequest(f"max_retries must be between 0 and {MAX_RETRIES}")
    result = run_pipeline(
        insight, generator, verifier, pipeline, translator, max_retries,
        gate=gate, classifier=classifier,
    )
    result["models"] = {
        "generator": generator,
        "verifier": verifier,
        "translator": translator if pipeline == "translate" else None,
        "classifier": classifier if gate == "classifier" else None,
    }
    return result
