"""Self-improvement: a review list drives a stronger model to revise a configuration.

A review list holds generated surveys people flagged, each with a comment on what was
wrong and, optionally, what it should have been. An improvement loop

1. runs the base version (and the comparison version) on the list's insights and has
   a judge score each new survey against the comments;
2. asks the improver model for prompt edits and setting changes to the incumbent;
3. saves the result as a draft version whose parent is the incumbent, runs and
   judges it, and keeps it as the incumbent only if its score is higher;
4. repeats for the requested rounds, then compares the best version with the
   comparison version.

Every evaluation is an ordinary experiment, so its runs open in Compare. Nothing is
named, saved as runnable or activated automatically: the best version stays a draft
until a person saves it in Configure.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, ValidationError, model_validator
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from mel.agents import Providers, build_agents
from mel.config import ModelConfig, Settings, StrictModel
from mel.configs import config_bundle, validate_definition
from mel.costs import CostedModel, RunCosts, request_scope
from mel.experiments import ExperimentSpec, candidate_run_id, execute_experiment

IMPROVE_PROMPT = Path("prompts/improve.txt")
JUDGE_PROMPT = Path("prompts/judge.txt")
MAX_INSIGHTS = 50  # An experiment compares at most fifty insights.
JUDGE_CONCURRENCY = 4
OUTPUT_RETRIES = 3

# Settings the improver may change. Model choices also need MEL_ALLOWED_MODELS.
SettingPath = Literal[
    "generation.model_id",
    "generation.api",
    "generation.reasoning_effort",
    "generation.temperature",
    "generation.max_tokens",
    "review.model_id",
    "review.api",
    "review.reasoning_effort",
    "review.temperature",
    "review.max_tokens",
    "graph.max_revisions",
    "graph.relevance_threshold",
]
MODEL_PATHS = frozenset({"generation.model_id", "generation.api", "review.model_id", "review.api"})


def review_target(outcome: dict, question_id: str | None) -> dict:
    """The survey a review comment is about: the accepted survey or the rejected draft."""
    survey = outcome.get("survey") or outcome.get("candidate_survey")
    if survey is None:
        raise ValueError("this result has no survey to review")
    if question_id is not None and not any(
        item["item_id"] == question_id for item in survey["items"]
    ):
        raise ValueError("question does not belong to this survey")
    return survey


class ReviewComment(StrictModel):
    """One review list item as the loop sees it, frozen when the loop is queued."""

    item_id: str
    insight_id: str
    question_id: str | None
    comment: str
    should_be: str | None
    insight: dict
    survey: dict


class ImprovementSpec(StrictModel):
    list_id: UUID
    base_config_id: UUID
    compare_config_id: UUID
    improver: ModelConfig
    rounds: int = Field(ge=1, le=5)
    budget_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    items: list[ReviewComment] = Field(min_length=1)

    @model_validator(mode="after")
    def runnable(self):
        if not self.improver.model_id:
            raise ValueError("choose an improver model")
        insights = review_insights(self.items)
        if len(insights) > MAX_INSIGHTS:
            raise ValueError(f"a review list can drive at most {MAX_INSIGHTS} insights")
        return self


def review_insights(items: list[ReviewComment]) -> list[dict]:
    """Each reviewed insight once, in the order first commented on."""
    insights: dict[str, dict] = {}
    for item in items:
        previous = insights.setdefault(item.insight_id, item.insight)
        if previous != item.insight:
            raise ValueError(f"insight {item.insight_id} appears with different source records")
    return list(insights.values())


class PromptEdit(StrictModel):
    prompt: Literal["generate", "review"]
    find: str = Field(
        min_length=1, description="Exact text from the current prompt; must occur once."
    )
    replace: str = Field(description="Replacement text. Empty deletes the found text.")
    reason: str = Field(min_length=1, max_length=500)


class SettingChange(StrictModel):
    path: SettingPath
    value: str | int | float | None
    reason: str = Field(min_length=1, max_length=500)


class ConfigProposal(StrictModel):
    summary: str = Field(min_length=1, max_length=300)
    prompt_edits: list[PromptEdit] = Field(default_factory=list, max_length=12)
    setting_changes: list[SettingChange] = Field(default_factory=list, max_length=8)


class Judgement(StrictModel):
    item_id: str
    score: float = Field(ge=0, le=1)
    addressed: bool
    reason: str = Field(min_length=1, max_length=500)


class Judgements(StrictModel):
    judgements: list[Judgement]


def apply_proposal(definition: dict, proposal: ConfigProposal, settings: Settings) -> dict:
    """The definition with the proposal applied, validated as a runnable version.
    Raises ValueError (or ValidationError) for anything that cannot be applied."""
    changed = copy.deepcopy(definition)
    for edit in proposal.prompt_edits:
        text = changed["prompts"][edit.prompt]
        found = text.count(edit.find)
        if found != 1:
            raise ValueError(
                f"the {edit.prompt} prompt contains the `find` text {found} times; "
                "copy one passage verbatim that occurs exactly once"
            )
        changed["prompts"][edit.prompt] = text.replace(edit.find, edit.replace)
    for change in proposal.setting_changes:
        if change.path in MODEL_PATHS and not settings.allowed_models:
            raise ValueError("model choices are not configured; models cannot change")
        section, field = change.path.split(".")
        changed["config"][section][field] = change.value
    if changed == definition:
        raise ValueError("the proposal changes nothing")
    return validate_definition(changed, settings).definition


@dataclass
class ProposalContext:
    definition: dict
    settings: Settings


def improver_agents(
    improver: Model, judge: Model, model_settings: dict
) -> tuple[Agent[ProposalContext, ConfigProposal], Agent[set[str], Judgements]]:
    """The improver and judge agents for already-constructed models. Their output
    validators send inapplicable proposals and incomplete judgements back for repair."""
    propose = Agent(
        improver,
        output_type=ConfigProposal,
        deps_type=ProposalContext,
        instructions=IMPROVE_PROMPT.read_text(),
        model_settings=model_settings,
        output_retries=OUTPUT_RETRIES,
    )
    grade = Agent(
        judge,
        output_type=Judgements,
        deps_type=set[str],
        instructions=JUDGE_PROMPT.read_text(),
        model_settings=model_settings,
        output_retries=OUTPUT_RETRIES,
    )

    @propose.output_validator
    def applicable(ctx: RunContext[ProposalContext], proposal: ConfigProposal) -> ConfigProposal:
        try:
            apply_proposal(ctx.deps.definition, proposal, ctx.deps.settings)
        except (ValueError, ValidationError) as error:
            raise ModelRetry(f"This proposal cannot be applied: {error}") from None
        return proposal

    @grade.output_validator
    def complete(ctx: RunContext[set[str]], result: Judgements) -> Judgements:
        judged = [j.item_id for j in result.judgements]
        if sorted(judged) != sorted(ctx.deps):
            raise ModelRetry(f"Return exactly one judgement for each item_id: {sorted(ctx.deps)}")
        return result

    return propose, grade


def build_improver_agents(model: ModelConfig, region: str, project_id: str):
    native, model_settings = Providers(region, project_id).model(model)
    return improver_agents(native, native, model_settings)


def _metered(agent: Agent, store, run_id: str, model: ModelConfig, region: str, role: str):
    """Charge the agent's requests to a run, under its budget and the console cap.
    In-memory stores have no request accounting."""
    if not hasattr(store, "connection"):
        return agent.override()
    return agent.override(
        model=CostedModel(agent.model, RunCosts(store, run_id), model, region, role)
    )


def _survey_items(survey: dict | None) -> list[dict] | None:
    return survey["items"] if survey else None


async def judge_run(
    grade: Agent, items: list[ReviewComment], outcomes: dict[str, dict], meter
) -> list[dict]:
    """Score each comment against the new outcome for its insight. Only accepted
    surveys can address a comment; anything else scores zero without a model call."""
    by_insight: dict[str, list[ReviewComment]] = {}
    for item in items:
        by_insight.setdefault(item.insight_id, []).append(item)
    gate = asyncio.Semaphore(JUDGE_CONCURRENCY)

    async def one(insight_id: str, comments: list[ReviewComment]) -> list[dict]:
        outcome = outcomes.get(insight_id)
        status = outcome["status"] if outcome else "missing"
        if status != "accepted":
            return [
                {
                    "item_id": c.item_id,
                    "insight_id": insight_id,
                    "score": 0.0,
                    "addressed": False,
                    "reason": f"No accepted survey ({status}).",
                    "status": status,
                }
                for c in comments
            ]
        prompt = json.dumps(
            {
                "insight": comments[0].insight,
                "feedback": [
                    {
                        "item_id": c.item_id,
                        "flagged_survey": c.survey["items"],
                        "flagged_question": c.question_id,
                        "comment": c.comment,
                        "should_be": c.should_be,
                    }
                    for c in comments
                ],
                "new_survey": outcome["survey"]["items"],
            },
            ensure_ascii=False,
        )
        async with gate:
            with request_scope(insight_id), meter():
                result = await grade.run(prompt, deps={c.item_id for c in comments})
        return [
            {**j.model_dump(), "insight_id": insight_id, "status": status}
            for j in result.output.judgements
        ]

    groups = await asyncio.gather(*(one(key, group) for key, group in by_insight.items()))
    order = {item.item_id: index for index, item in enumerate(items)}
    return sorted((j for group in groups for j in group), key=lambda j: order[j["item_id"]])


@dataclass
class Evaluation:
    label: str
    config_id: str
    experiment_id: str
    run_id: str
    outcomes: dict[str, dict]
    judgements: list[dict]

    @property
    def score(self) -> float:
        return sum(j["score"] for j in self.judgements) / len(self.judgements)

    def summary(self) -> dict:
        counts = Counter(o["status"] for o in self.outcomes.values())
        relevance = [
            o["review"]["relevance_score"] for o in self.outcomes.values() if o.get("review")
        ]
        return {
            "label": self.label,
            "config_id": self.config_id,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "score": round(self.score, 4),
            "addressed": sum(j["addressed"] for j in self.judgements),
            "comments": len(self.judgements),
            "counts": dict(sorted(counts.items())),
            "mean_relevance": round(sum(relevance) / len(relevance), 4) if relevance else None,
            "judgements": self.judgements,
        }


def proposal_context(
    evaluation: Evaluation,
    definition: dict,
    items: list[ReviewComment],
    history: list[dict],
    settings: Settings,
) -> str:
    """The improver's input: the incumbent, every comment with its current result,
    and earlier attempts in this loop."""
    judged = {j["item_id"]: j for j in evaluation.judgements}
    config = definition["config"]
    return json.dumps(
        {
            "current_version": {
                "generation": config["generation"],
                "review": config["review"],
                "graph": {
                    key: config["graph"][key] for key in ("max_revisions", "relevance_threshold")
                },
                "prompts": definition["prompts"],
            },
            "model_choices": [m.model_dump() for m in settings.allowed_models],
            "current_score": round(evaluation.score, 4),
            "review_items": [
                {
                    "item_id": item.item_id,
                    "insight": item.insight,
                    "flagged_survey": item.survey["items"],
                    "flagged_question": item.question_id,
                    "comment": item.comment,
                    "should_be": item.should_be,
                    "current_result": {
                        "status": evaluation.outcomes.get(item.insight_id, {}).get("status"),
                        "survey": _survey_items(
                            evaluation.outcomes.get(item.insight_id, {}).get("survey")
                            or evaluation.outcomes.get(item.insight_id, {}).get("candidate_survey")
                        ),
                        "reviewer_issues": (
                            evaluation.outcomes.get(item.insight_id, {}).get("review") or {}
                        ).get("issues"),
                        "judge_score": judged[item.item_id]["score"],
                        "judge_reason": judged[item.item_id]["reason"],
                    },
                }
                for item in items
            ],
            "previous_attempts": history,
        },
        ensure_ascii=False,
    )


async def execute_improvement(
    spec: ImprovementSpec,
    *,
    store,
    settings: Settings,
    improvement_id: str,
    job_id: str | None = None,
    agent_factory=build_agents,
    improver_factory=build_improver_agents,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Run the loop to a verdict, recording each round as it finishes. Failures mark
    the improvement failed and propagate to the job."""
    region = settings.aws_region
    items = spec.items
    insights = review_insights(items)
    base_id, compare_id = str(spec.base_config_id), str(spec.compare_config_id)
    total = 1 + (compare_id != base_id) + spec.rounds
    done = 0
    propose, grade = improver_factory(spec.improver, region, settings.bedrock_project_id)
    await asyncio.to_thread(store.start_improvement, improvement_id)

    async def evaluate(label: str, config_id: str) -> Evaluation:
        nonlocal done
        key = str(uuid5(NAMESPACE_URL, f"improvement/{improvement_id}/{config_id}"))
        await execute_experiment(
            ExperimentSpec(
                name=f"Improvement {improvement_id[:8]} · {label}",
                insights=insights,
                candidates=[{"label": label, "config_id": config_id}],
                budget_usd=spec.budget_usd,
            ),
            settings=settings,
            store=store,
            experiment_id=key,
            job_id=job_id,
            agent_factory=agent_factory,
        )
        run_id = candidate_run_id(key, 0)
        outcomes = {o["insight_id"]: o for o in await asyncio.to_thread(store.results, run_id)}
        judgements = await judge_run(
            grade,
            items,
            outcomes,
            lambda: _metered(grade, store, run_id, spec.improver, region, "judge"),
        )
        done += 1
        if on_progress is not None:
            await asyncio.to_thread(on_progress, done, total)
        return Evaluation(label, config_id, key, run_id, outcomes, judgements)

    try:
        base = await evaluate("Base", base_id)
        compare = base if compare_id == base_id else await evaluate("Comparison", compare_id)
        await asyncio.to_thread(
            store.add_improvement_round,
            improvement_id,
            {
                "round": 0,
                "evaluations": [base.summary()]
                + ([compare.summary()] if compare is not base else []),
            },
        )
        incumbent = base
        history: list[dict] = []
        root_version = (await asyncio.to_thread(config_bundle, store, base_id)).config.version
        for number in range(1, spec.rounds + 1):
            definition = (
                await asyncio.to_thread(config_bundle, store, incumbent.config_id)
            ).definition
            with (
                request_scope("improvement", number),
                _metered(propose, store, incumbent.run_id, spec.improver, region, "improve"),
            ):
                result = await propose.run(
                    proposal_context(incumbent, definition, items, history, settings),
                    deps=ProposalContext(definition, settings),
                )
            proposal = result.output
            candidate = apply_proposal(definition, proposal, settings)
            candidate["config"]["version"] = (
                f"{root_version}-improve-{improvement_id[:8]}-r{number}"
            )
            config_id = await asyncio.to_thread(
                store.save_config,
                candidate,
                parent_id=incumbent.config_id,
                note=f"Improvement round {number}: {proposal.summary}",
            )
            evaluation = await evaluate(f"Round {number}", config_id)
            kept = evaluation.score > incumbent.score
            attempt = {
                "round": number,
                "proposal": proposal.model_dump(),
                "parent_config_id": incumbent.config_id,
                "parent_score": round(incumbent.score, 4),
                "kept": kept,
                "evaluations": [evaluation.summary()],
            }
            await asyncio.to_thread(store.add_improvement_round, improvement_id, attempt)
            history.append(
                {
                    "round": number,
                    "summary": proposal.summary,
                    "prompt_edits": [e.model_dump() for e in proposal.prompt_edits],
                    "setting_changes": [c.model_dump() for c in proposal.setting_changes],
                    "score": round(evaluation.score, 4),
                    "kept": kept,
                }
            )
            if kept:
                incumbent = evaluation
        verdict = {
            "best": incumbent.summary(),
            "compare": compare.summary(),
            "improved": incumbent.score > compare.score,
            "score_delta": round(incumbent.score - compare.score, 4),
            "note": (
                "Scores are a model's judgement of how well each survey addresses the "
                "review comments on these insights only. Run a comparison on a wider "
                "selection before saving or activating the version."
            ),
        }
        await asyncio.to_thread(
            store.finish_improvement,
            improvement_id,
            "completed",
            verdict=verdict,
            best_config_id=incumbent.config_id,
        )
        return verdict
    except Exception as error:
        await asyncio.to_thread(
            store.finish_improvement,
            improvement_id,
            "failed",
            error=f"{type(error).__name__}: {error}",
        )
        raise
