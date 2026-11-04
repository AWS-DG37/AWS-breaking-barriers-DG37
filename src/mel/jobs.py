"""Background jobs: long work the API queues and a runner executes.

A job row names its kind and spec. `run_job` dispatches on the kind; `run_queue` claims
queued jobs one at a time. Locally the runner is `mel-worker jobs`; in AWS the same
command runs as a Fargate task. Several runners can share one queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from mel.agents import build_agents
from mel.config import Settings, StrictModel
from mel.configs import config_bundle
from mel.datasets import InsightFilter
from mel.experiments import ExperimentSpec, execute_experiment
from mel.store import Store
from mel.worker import execute_run

logger = logging.getLogger("mel.jobs")


class BatchJobSpec(StrictModel):
    """Generate surveys for a dataset selection with one configuration version."""

    dataset_id: UUID
    config_id: UUID
    selection: InsightFilter = Field(default_factory=InsightFilter)


class ExperimentJobSpec(StrictModel):
    experiment_id: UUID


async def run_job(job: dict, *, store: Store, settings: Settings, agent_factory=build_agents):
    """Run one claimed job to a final status. Errors are recorded, not raised, except
    cancellation, which leaves the job `running` so `--resume` can pick it up."""
    try:
        if job["kind"] == "batch":
            status = await _batch(job, store, settings, agent_factory)
        elif job["kind"] == "experiment":
            status = await _experiment(job, store, settings, agent_factory)
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
    )
    detail = await asyncio.to_thread(store.experiment_detail, key)
    return detail["status"]


async def run_queue(
    store: Store,
    settings: Settings,
    *,
    once: bool = False,
    poll_seconds: float = 2.0,
    job_id: str | None = None,
    resume: bool = False,
    agent_factory=build_agents,
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
        await run_job(job, store=store, settings=settings, agent_factory=agent_factory)
        ran += 1
        if job_id is not None:
            return ran
