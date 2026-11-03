"""Readable topology: Select -> Generate -> ReviewSurvey -> Accept / Revise / Reject.

Edit node edges here. Tune thresholds and loop limits in config/agent.yaml.
Prompts live in prompts/. Domain timing/channel rules live in rules.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from mel.config import AgentConfig
from mel.contracts import Contracts
from mel.models import Outcome, Review
from mel.policy import eligibility, handoff, send_window


@dataclass
class State:
    insight: dict
    survey_id: str
    now: datetime
    attempts: int = 0
    feedback: list[str] = field(default_factory=list)
    survey: dict | None = None
    window: dict | None = None
    review: Review | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    usage_complete: bool = True


@dataclass
class Dependencies:
    config: AgentConfig
    rules: dict
    calendar: list[dict]
    contracts: Contracts
    generator: Agent
    reviewer: Agent


def finish(state: State, status: str, **kwargs) -> End[Outcome]:
    return End(
        Outcome(
            insight_id=state.insight["insight_id"],
            status=status,
            attempts=state.attempts,
            input_tokens=state.usage.input_tokens,
            output_tokens=state.usage.output_tokens,
            usage_complete=state.usage_complete,
            review=state.review,
            **kwargs,
        )
    )


def check_usage(state: State, result) -> None:
    # A completed text/tool response cannot genuinely consume zero tokens.
    # Missing counts are unknown usage, not free inference. Do not invent totals.
    for message in result.new_messages():
        if isinstance(message, ModelResponse):
            if message.usage.input_tokens == 0 and message.usage.output_tokens == 0:
                state.usage_complete = False


@dataclass
class Select(BaseNode[State, Dependencies, Outcome]):
    async def run(self, ctx: GraphRunContext[State, Dependencies]) -> Generate | End[Outcome]:
        s, d = ctx.state, ctx.deps
        if s.insight.get("quality_flag") == "junk":
            return finish(s, "skipped", reason="junk_insight")
        errors = d.contracts.errors("insight", s.insight)
        if errors:
            return finish(s, "skipped", reason="invalid_insight: " + "; ".join(errors))
        reason = eligibility(s.insight, d.rules)
        if reason:
            no_survey = None
            # Pack v1's no_survey schema cannot represent junk/invalid/verification skips.
            if reason in ("generic_information", "needs_more_context"):
                no_survey = {
                    "insight_id": s.insight["insight_id"],
                    "farmer_id": s.insight["farmer_id"],
                    "decision": "no_survey",
                    "reason_code": reason,
                    "reason": d.rules["survey_by_resolution"][reason]["asks"],
                }
                d.contracts.validate("no_survey", no_survey)
            return finish(s, "skipped", reason=reason, no_survey=no_survey)
        try:
            s.window = send_window(s.insight, d.rules)
        except ValueError as error:
            return finish(s, "skipped", reason=str(error))
        return Generate()


@dataclass
class Generate(BaseNode[State, Dependencies, Outcome]):
    async def run(self, ctx: GraphRunContext[State, Dependencies]) -> ReviewSurvey:
        s, d = ctx.state, ctx.deps
        s.attempts += 1
        crop = s.insight["asset"]["name"]
        calendar = [row for row in d.calendar if row["crop"] == crop]
        result = await d.generator.run(
            json.dumps(
                {
                    "insight": s.insight,
                    "language": s.insight["conversation_language"],
                    "send_window": s.window,
                    "crop_calendar": calendar,
                    "previous_draft": s.survey,
                    "reviewer_feedback": s.feedback,
                },
                ensure_ascii=False,
            ),
            usage=s.usage,
            usage_limits=UsageLimits(
                request_limit=s.usage.requests + d.config.graph.max_requests_per_call
            ),
        )
        check_usage(s, result)
        s.survey = {
            "survey_id": s.survey_id,
            "farmer_id": s.insight["farmer_id"],
            "source_insight_ids": [s.insight["insight_id"]],
            "language": s.insight["conversation_language"],
            "send_window": s.window,
            **result.output.model_dump(exclude_none=True),
        }
        return ReviewSurvey()


@dataclass
class ReviewSurvey(BaseNode[State, Dependencies, Outcome]):
    async def run(self, ctx: GraphRunContext[State, Dependencies]) -> Generate | End[Outcome]:
        s, d = ctx.state, ctx.deps
        s.feedback = d.contracts.errors("survey", s.survey)
        if not s.feedback:
            result = await d.reviewer.run(
                json.dumps(
                    {
                        "insight": s.insight,
                        "survey": s.survey,
                        "constraints": {"max_questions": 3, "conditional_display_supported": False},
                    },
                    ensure_ascii=False,
                ),
                usage=s.usage,
                usage_limits=UsageLimits(
                    request_limit=s.usage.requests + d.config.graph.max_requests_per_call
                ),
            )
            check_usage(s, result)
            s.review = result.output
            r = s.review
            if (
                r.relevance_score >= d.config.graph.relevance_threshold
                and r.language_correct
                and not r.gives_new_advice
                and not r.asks_personal_info
                and not r.issues
            ):
                s.survey["self_eval"] = {
                    "schema_valid": True,
                    "relevance_score": r.relevance_score,
                    "gives_new_advice": r.gives_new_advice,
                    "asks_personal_info": r.asks_personal_info,
                }
                payload = handoff(s.survey, s.insight, d.rules, s.now)
                d.contracts.validate("survey", s.survey)
                d.contracts.validate("schedule_handoff", payload)
                return finish(s, "accepted", survey=s.survey, handoff=payload)
            s.feedback = r.issues + [
                f"relevance={r.relevance_score}; correct_language={r.language_correct}; "
                f"new_advice={r.gives_new_advice}; personal_info={r.asks_personal_info}"
            ]
        if s.attempts <= d.config.graph.max_revisions:
            return Generate()
        return finish(s, "rejected", reason="; ".join(s.feedback), candidate_survey=s.survey)


survey_graph = Graph(nodes=[Select, Generate, ReviewSurvey], name="survey_generation")
