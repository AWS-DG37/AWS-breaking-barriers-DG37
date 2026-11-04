import asyncio
import json
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from mel.bundle import Bundle
from mel.config import Settings
from mel.graph import Dependencies
from mel.models import Review, SurveyDraft
from mel.worker import process_insight


def dependencies(review=None):
    bundle = Bundle.load(Settings(bedrock_model_id="test"))
    survey = json.loads(
        Path(
            "docs/participant-pack/json/scenarios/1-mustard-aphid-spray/survey.hi-IN.json"
        ).read_text()
    )
    draft = {
        "items": survey["items"],
        "rationale": survey["rationale"],
        "question_evidence": [
            {
                "item_id": survey["items"][0]["item_id"],
                "measurement": "adoption",
                "source_fields": ["advice_summary"],
                "explanation": "Asks about doing the advised action.",
            }
        ],
    }
    good = {
        "relevance_score": 0.95,
        "language_correct": True,
        "gives_new_advice": False,
        "asks_personal_info": False,
        "issues": [],
    }
    return Dependencies(
        bundle.config,
        bundle.snapshot["rules"],
        bundle.snapshot["calendar"],
        bundle.contracts,
        Agent(TestModel(custom_output_args=draft), output_type=SurveyDraft),
        Agent(TestModel(custom_output_args=review or good), output_type=Review),
    )


def insight():
    return json.loads(
        Path("docs/participant-pack/json/scenarios/1-mustard-aphid-spray/insight.json").read_text()
    )


def test_trace_preserves_revisions_and_evidence_outside_survey():
    deps = dependencies(
        {
            "relevance_score": 0.1,
            "language_correct": True,
            "gives_new_advice": False,
            "asks_personal_info": False,
            "issues": ["Not grounded"],
        }
    )
    result = asyncio.run(process_insight(insight(), "S-0000000001", deps, context={"sources": []}))
    assert result.status == "rejected"
    assert [e.sequence for e in result.events] == list(range(1, len(result.events) + 1))
    drafts = [e for e in result.events if e.stage == "generate" and e.source == "model"]
    assert [e.attempt for e in drafts] == [1, 2]
    assert len([e for e in result.events if e.stage == "review" and e.source == "model"]) == 2
    assert result.question_evidence[0].measurement == "adoption"
    assert "question_evidence" not in result.candidate_survey


def test_failure_retains_observed_history_and_only_safe_error_type():
    deps = dependencies()
    deps.reviewer = None
    result = asyncio.run(process_insight(insight(), "S-0000000001", deps))
    assert result.status == "failed"
    assert any(e.stage == "generate" and e.source == "model" for e in result.events)
    assert result.events[-1].stage == "failure"
    assert result.events[-1].data == {"error_type": "AttributeError"}
    assert result.candidate_survey is not None
