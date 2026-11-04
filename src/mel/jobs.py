"""Background jobs: long work the API queues and a runner executes.

A job row names its kind and spec. `run_job` dispatches on the kind; `run_queue` claims
queued jobs one at a time. Locally the runner is `mel-worker jobs`; in AWS the same
command runs as a Fargate task. Several runners can share one queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from mel.agents import build_agents
from mel.bundle import Bundle
from mel.config import Settings, StrictModel
from mel.configs import config_bundle
from mel.datasets import InsightFilter
from mel.experiments import ExperimentSpec, execute_experiment
from mel.improvement import ImprovementSpec, build_improver_agents, execute_improvement
from mel.store import Store
from mel.worker import execute_run

logger = logging.getLogger("mel.jobs")


def dispatch(settings: Settings, job_id: str) -> None:
    """Start a runner for one queued job when a job cluster is configured. Otherwise
    a polling runner claims it; the job row is the same either way."""
    if settings.job_cluster is None:
        return
    import boto3

    response = boto3.client("ecs", region_name=settings.aws_region).run_task(
        cluster=settings.job_cluster,
        taskDefinition=settings.job_task_definition,
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": settings.job_subnets,
                "securityGroups": settings.job_security_groups,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {"name": settings.job_container, "command": ["jobs", "--job-id", job_id]}
            ]
        },
        startedBy=f"job-{job_id}"[:36],
    )
    if response.get("failures"):
        raise RuntimeError(f"RunTask failed: {response['failures']}")


class BatchJobSpec(StrictModel):
    """Generate surveys for a dataset selection with one configuration version."""

    budget_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    dataset_id: UUID
    config_id: UUID
    selection: InsightFilter = Field(default_factory=InsightFilter)


class ExperimentJobSpec(StrictModel):
    experiment_id: UUID


class ImprovementJobSpec(StrictModel):
    improvement_id: UUID


# Rough per-survey figures from a live GPT-6 Luna run (one generation, one review).
# They size a run before it starts; actual usage is recorded per outcome.
ESTIMATE_SECONDS_PER_SURVEY = 12
ESTIMATE_INPUT_TOKENS_PER_SURVEY = 6600
ESTIMATE_OUTPUT_TOKENS_PER_SURVEY = 400


def estimate_batch(bundle: Bundle, insights: list[dict]) -> dict:
    """What a batch would do, decided by the rules alone: no model calls. Insights the
    rules decline are skipped for free; each survey costs one generation and one
    review, plus up to max_revisions more of each."""
    engine = bundle.decision_engine()
    rules = Counter(engine.decide(insight).rule for insight in insights)
    surveys = rules.pop(engine.survey_rule, 0)
    config = bundle.config
    input_tokens = surveys * ESTIMATE_INPUT_TOKENS_PER_SURVEY
    output_tokens = surveys * ESTIMATE_OUTPUT_TOKENS_PER_SURVEY
    prices = config.batch
    cost = (
        (
            input_tokens * prices.input_cost_per_million
            + output_tokens * prices.output_cost_per_million
        )
        / 1_000_000
        if prices.input_cost_per_million is not None and prices.output_cost_per_million is not None
        else None
    )
    return {
        "selected": len(insights),
        "surveys": surveys,
        "skipped": dict(sorted(rules.items())),
        "model_calls": {
            "min": 2 * surveys,
            "max": 2
            * surveys
            * (1 + config.graph.max_revisions)
            * config.graph.max_requests_per_call,
        },
        "minutes": round(surveys * ESTIMATE_SECONDS_PER_SURVEY / config.batch.concurrency / 60, 1),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 2) if cost is not None else None,
    }


async def run_job(
    job: dict,
    *,
    store: Store,
    settings: Settings,
    agent_factory=build_agents,
    improver_factory=build_improver_agents,
):
    """Run one claimed job to a final status. Errors are recorded, not raised, except
    cancellation, which leaves the job `running` so `--resume` can pick it up."""
    try:
        if job["kind"] == "batch":
            status = await _batch(job, store, settings, agent_factory)
        elif job["kind"] == "experiment":
            status = await _experiment(job, store, settings, agent_factory)
        elif job["kind"] == "improve":
            status = await _improve(job, store, settings, agent_factory, improver_factory)
        else:
            raise ValueError(f"unknown job kind {job['kind']}")
        await asyncio.to_thread(store.finish_job, job["job_id"], status)
    except Exception as error:
        logger.error(json.dumps({"event": "job_failed", "job_id": job["job_id"]}))
        await asyncio.to_thread(
            store.finish_job, job["job_id"], "failed", f"{type(error).__name__}: {error}"
        )


async def _batch(job, store, settings, agent_factory) -> str:
    spec = BatchJobSpec.model_validate(job["spec"])
    config_id = str(spec.config_id)
    bundle = await asyncio.to_thread(config_bundle, store, config_id)
    insights = await asyncio.to_thread(store.insights, str(spec.dataset_id), spec.selection)
    if not insights:
        raise ValueError("the dataset selection contains no insights")
    selection = spec.model_dump(mode="json", exclude={"config_id"})
    results = await execute_run(
        bundle,
        insights,
        # One run per job, so resuming the job resumes the same checkpoints.
        run_id=str(uuid5(NAMESPACE_URL, f"job/{job['job_id']}")),
        fingerprint_body=json.dumps(selection, sort_keys=True).encode(),
        selection=selection,
        store=store,
        settings=settings,
        config_id=config_id,
        job_id=job["job_id"],
        budget_usd=spec.budget_usd,
        agent_factory=agent_factory,
        on_progress=lambda done, total: store.job_progress(
            job["job_id"], {"done": done, "total": total}
        ),
    )
    return "completed_with_errors" if any(r["status"] == "failed" for r in results) else "completed"


async def _experiment(job, store, settings, agent_factory) -> str:
    key = str(ExperimentJobSpec.model_validate(job["spec"]).experiment_id)
    detail = await asyncio.to_thread(store.experiment_detail, key)
    if detail is None:
        raise ValueError(f"unknown experiment {key}")
    await execute_experiment(
        ExperimentSpec.model_validate(detail["spec"]),
        settings=settings,
        store=store,
        experiment_id=key,
        job_id=job["job_id"],
        agent_factory=agent_factory,
        on_progress=lambda done, total: store.job_progress(
            job["job_id"], {"done": done, "total": total}
        ),
    )
    detail = await asyncio.to_thread(store.experiment_detail, key)
    return detail["status"]


async def _improve(job, store, settings, agent_factory, improver_factory) -> str:
    key = str(ImprovementJobSpec.model_validate(job["spec"]).improvement_id)
    record = await asyncio.to_thread(store.improvement, key)
    if record is None:
        raise ValueError(f"unknown improvement {key}")
    await execute_improvement(
        ImprovementSpec.model_validate(record["spec"]),
        store=store,
        settings=settings,
        improvement_id=key,
        job_id=job["job_id"],
        agent_factory=agent_factory,
        improver_factory=improver_factory,
        on_progress=lambda done, total: store.job_progress(
            job["job_id"], {"done": done, "total": total, "unit": "evaluations"}
        ),
    )
    return "completed"


async def run_queue(
    store: Store,
    settings: Settings,
    *,
    once: bool = False,
    poll_seconds: float = 2.0,
    job_id: str | None = None,
    resume: bool = False,
    agent_factory=build_agents,
    improver_factory=build_improver_agents,
) -> int:
    """Claim and run jobs until the queue is empty (`once`) or forever. Returns how
    many jobs ran. A named `job_id` runs only that job. The caller owns `store`."""
    ran = 0
    while True:
        job = await asyncio.to_thread(store.claim_job, job_id, resume=resume)
        if job is None:
            if once or job_id is not None:
                return ran
            await asyncio.sleep(poll_seconds)
            continue
        logger.info(json.dumps({"event": "job_started", "job_id": job["job_id"]}))
        await run_job(
            job,
            store=store,
            settings=settings,
            agent_factory=agent_factory,
            improver_factory=improver_factory,
        )
        ran += 1
        if job_id is not None:
            return ran
