"""Readable topology: Select -> Generate -> ReviewSurvey -> Accept / Revise / Reject.

Edit node edges here. Tune thresholds and loop limits in config/agent.yaml.
Prompts live in prompts/. Domain timing/channel rules live in rules.json.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from mel.bundle import Bundle
from mel.config import AgentConfig
from mel.contracts import Contracts
from mel.costs import request_scope
from mel.decision import CropCalendar, Decision, DecisionEngine
from mel.handoff import handoff
from mel.models import Outcome, QuestionEvidence, Review, TraceEvent


@dataclass
class State:
    insight: dict
    survey_id: str
    now: datetime
    attempts: int = 0
    feedback: list[str] = field(default_factory=list)
    decision: Decision | None = None
    survey: dict | None = None
    window: dict | None = None
    review: Review | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    usage_complete: bool = True
    context: dict | None = None
    events: list[TraceEvent] = field(default_factory=list)
    question_evidence: list[QuestionEvidence] = field(default_factory=list)

    def record(self, stage: str, source: str, **data) -> None:
        self.events.append(
            TraceEvent(
                sequence=len(self.events) + 1,
                stage=stage,
                source=source,
                attempt=self.attempts,
                data=copy.deepcopy(data),
            )
        )


@dataclass
class Dependencies:
    config: AgentConfig
    rules: dict
    calendar: CropCalendar
    contracts: Contracts
    decisions: DecisionEngine
    generator: Agent
    reviewer: Agent

    @classmethod
    def from_bundle(cls, bundle: Bundle, generator: Agent, reviewer: Agent) -> Dependencies:
        return cls(
            bundle.config,
            bundle.snapshot["rules"],
            bundle.calendar(),
            bundle.contracts,
            bundle.decision_engine(),
            generator,
            reviewer,
        )


def finish(state: State, status: str, **kwargs) -> End[Outcome]:
    state.record("decision", "rule", status=status, reason=kwargs.get("reason"))
    return End(
        Outcome(
            events=state.events,
            question_evidence=state.question_evidence,
            insight_id=state.insight["insight_id"],
            status=status,
            rule=state.decision.rule if state.decision else None,
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


def no_survey(insight: dict, decision: Decision) -> dict | None:
    """The pack's no_survey contract object, for the reasons that contract can carry."""
    if decision.no_survey_code is None:
        return None
    return {
        "insight_id": insight["insight_id"],
        "farmer_id": insight["farmer_id"],
        "decision": "no_survey",
        "reason_code": decision.no_survey_code,
        "reason": decision.explanation,
    }


@dataclass
class Select(BaseNode[State, Dependencies, Outcome]):
    async def run(self, ctx: GraphRunContext[State, Dependencies]) -> Generate | End[Outcome]:
        s, d = ctx.state, ctx.deps
        s.record("select", "runtime", insight=s.insight)
        if s.context is not None:
            s.record("context", "runtime", context=s.context)
        decision = s.decision = d.decisions.decide(s.insight)
        s.record(
            "select",
            "rule",
            rule=decision.rule,
            eligible=decision.survey,
            reason=decision.reason_code,
            explanation=decision.explanation,
            send_window=decision.window.to_contract() if decision.window else None,
        )
        if not decision.survey:
            contract = no_survey(s.insight, decision)
            if contract is not None:
                d.contracts.validate("no_survey", contract)
            return finish(
                s,
                "skipped",
                reason=decision.reason_code,
                detail=decision.explanation,
                no_survey=contract,
            )
        s.window = decision.window.to_contract()
        return Generate()


@dataclass
class Generate(BaseNode[State, Dependencies, Outcome]):
    async def run(self, ctx: GraphRunContext[State, Dependencies]) -> ReviewSurvey:
        s, d = ctx.state, ctx.deps
        s.attempts += 1
        calendar = d.calendar.rows_for(s.insight["asset"]["name"])
        payload = {
            "insight": s.insight,
            "language": s.insight["conversation_language"],
            "send_window": s.window,
            "crop_calendar": calendar,
            "previous_draft": s.survey,
            "reviewer_feedback": s.feedback,
            "context": s.context,
        }
        s.record("generate", "runtime", kind="request", payload=payload)
        with request_scope(s.insight["insight_id"], s.attempts):
            result = await d.generator.run(
                json.dumps(payload, ensure_ascii=False),
                usage=s.usage,
                usage_limits=UsageLimits(
                    request_limit=s.usage.requests + d.config.graph.max_requests_per_call
                ),
            )
        check_usage(s, result)
        s.question_evidence = list(result.output.question_evidence)
        s.survey = {
            "survey_id": s.survey_id,
            "farmer_id": s.insight["farmer_id"],
            "source_insight_ids": [s.insight["insight_id"]],
            "language": s.insight["conversation_language"],
            "send_window": s.window,
            **result.output.model_dump(exclude_none=True, exclude={"question_evidence"}),
        }
        s.record(
            "generate",
            "model",
            kind="response",
            survey=s.survey,
            question_evidence=[q.model_dump() for q in s.question_evidence],
        )
        return ReviewSurvey()


@dataclass
class ReviewSurvey(BaseNode[State, Dependencies, Outcome]):
    async def run(self, ctx: GraphRunContext[State, Dependencies]) -> Generate | End[Outcome]:
        s, d = ctx.state, ctx.deps
        s.feedback = d.contracts.errors("survey", s.survey)
        s.record("schema", "schema", target="survey", valid=not s.feedback, errors=s.feedback)
        if not s.feedback:
            s.record("review", "runtime", kind="request", survey=s.survey)
            with request_scope(s.insight["insight_id"], s.attempts):
                result = await d.reviewer.run(
                    json.dumps(
                        {
                            "insight": s.insight,
                            "survey": s.survey,
                            "constraints": {
                                "max_questions": 3,
                                "conditional_display_supported": False,
                            },
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
            s.record("review", "model", kind="response", review=s.review.model_dump())
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
