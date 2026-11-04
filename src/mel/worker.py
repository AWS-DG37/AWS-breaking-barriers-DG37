"""Batch CLI. Container startup does no work until an explicit command is supplied."""

import argparse
import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from mel.agents import build_agents
from mel.bedrock import create_mantle_client
from mel.bundle import Bundle, local_bundle, read_bytes, write_bytes
from mel.clock import IST
from mel.config import Settings
from mel.costs import CostedModel, CostRefused, meter_agents
from mel.datasets import parse_insights
from mel.evaluation import (
    insights_jsonl,
    load_golden,
    load_results,
    score_decisions,
    score_surveys,
)
from mel.graph import Dependencies, Select, State, survey_graph
from mel.models import Outcome
from mel.store import Store

logger = logging.getLogger("mel")

DEFAULT_ROW_LIMIT = 10  # Rows processed unless --full is given.
GOLDEN_PATH = Path("docs/participant-pack/data/golden_set.jsonl")


async def close_model_clients(*agents) -> None:
    """Each native client is owned once, including distinct generator/reviewer clients."""
    seen = set()
    for agent in agents:
        model = getattr(agent, "model", None)
        while isinstance(model, CostedModel):
            model = model.wrapped
        client = getattr(model, "client", None)
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close:
            result = close()
            if inspect.isawaitable(result):
                await result


async def process_insight(
    insight: dict, survey_id: str, deps: Dependencies, *, context: dict | None = None
) -> Outcome:
    """The same generation/review loop for both local preview and persisted batches."""
    started = time.monotonic()
    state = State(insight, survey_id, datetime.now(IST), context=context)
    try:
        async with asyncio.timeout(deps.config.graph.insight_timeout_seconds):
            result = await survey_graph.run(Select(), state=state, deps=deps)
        outcome = result.output
    except CostRefused:
        raise
    except Exception as error:
        state.record("failure", "runtime", error_type=type(error).__name__)
        outcome = Outcome(
            events=state.events,
            question_evidence=state.question_evidence,
            candidate_survey=state.survey,
            review=state.review,
            insight_id=insight["insight_id"],
            status="failed",
            reason=type(error).__name__,
            attempts=state.attempts,
            input_tokens=state.usage.input_tokens,
            output_tokens=state.usage.output_tokens,
            usage_complete=False,
        )
    outcome.elapsed_seconds = round(time.monotonic() - started, 3)
    prices = deps.config.batch
    if (
        not isinstance(getattr(deps.generator, "model", None), CostedModel)
        and outcome.usage_complete
        and prices.input_cost_per_million is not None
        and prices.output_cost_per_million is not None
    ):
        outcome.estimated_cost_usd = (
            outcome.input_tokens * prices.input_cost_per_million
            + outcome.output_tokens * prices.output_cost_per_million
        ) / 1_000_000
    return outcome


async def check_model(settings: Settings) -> None:
    """One small inference request to check AWS credentials and model/project access."""
    async with create_mantle_client(settings.aws_region, settings.bedrock_project_id) as client:
        response = await client.responses.create(
            model=settings.bedrock_model_id,
            input="Reply with the single word OK.",
            max_output_tokens=512,
            reasoning={"effort": "low"},
            store=False,
        )
    if not response.output_text.strip():
        raise ValueError("Model responded without text; inspect access/settings and token budget")
    print(
        json.dumps(
            {
                "model": settings.bedrock_model_id,
                "project_id": settings.bedrock_project_id,
                "region": settings.aws_region,
                "reply": response.output_text,
                "usage": response.usage.model_dump() if response.usage is not None else None,
            }
        )
    )


async def execute_run(
    bundle: Bundle,
    insights: list[dict],
    *,
    run_id: str,
    fingerprint_body: bytes,
    selection: dict,
    store: Store,
    settings: Settings,
    config_id: str,
    job_id: str | None = None,
    budget_usd=None,
    retry_failed: bool = False,
    agent_factory=build_agents,
    on_progress=None,
) -> list[dict]:
    """One persisted handoff run: checkpointed, resumable, bounded by batch concurrency.

    The CLI `run` command and batch jobs both use this. `on_progress(done, total)` is
    called from a worker thread after each checkpoint.
    """
    config = bundle.config
    generator = reviewer = None
    run_started = False
    try:
        generator, reviewer = agent_factory(
            config, settings.aws_region, bundle.snapshot["prompts"], settings.bedrock_project_id
        )
        deps = Dependencies.from_bundle(bundle, generator, reviewer)
        store.start_run(
            run_id,
            bundle.fingerprint(fingerprint_body, settings.aws_region),
            {
                **bundle.snapshot,
                "bedrock_project_id": settings.bedrock_project_id,
                "selection": {**selection, "selected_rows": len(insights)},
            },
            config_id=config_id,
            job_id=job_id,
        )
        run_started = True
        if hasattr(store, "connection"):
            meter_agents(
                store, run_id, config, settings.aws_region, (generator, reviewer), budget_usd
            )
        logger.info(
            json.dumps(
                {
                    "event": "run_started",
                    "run_id": run_id,
                    "config_id": config_id,
                    "job_id": job_id,
                    **selection,
                    "selected_rows": len(insights),
                }
            )
        )
        done = store.completed(run_id, retry_failed)
        pending = iter(row for row in insights if row["insight_id"] not in done)
        finished = len(done)

        async def consume() -> None:
            nonlocal finished
            # Exactly concurrency workers; do not create thousands of waiting tasks.
            for insight in pending:
                outcome = await process_insight(
                    insight, await asyncio.to_thread(store.next_survey_id), deps
                )
                # Persistence failures abort the task. Never pretend uncheckpointed work succeeded.
                await asyncio.to_thread(store.save, run_id, insight, outcome)
                finished += 1
                if on_progress is not None:
                    await asyncio.to_thread(on_progress, finished, len(insights))
                logger.info(
                    json.dumps(
                        {
                            "event": "insight_completed",
                            "run_id": run_id,
                            "insight_id": insight["insight_id"],
                            "status": outcome.status,
                            "attempts": outcome.attempts,
                            "elapsed_seconds": outcome.elapsed_seconds,
                            "input_tokens": outcome.input_tokens,
                            "output_tokens": outcome.output_tokens,
                            "usage_complete": outcome.usage_complete,
                            "estimated_cost_usd": outcome.estimated_cost_usd,
                        }
                    )
                )

        async with asyncio.TaskGroup() as group:
            for _ in range(config.batch.concurrency):
                group.create_task(consume())
        results = store.results(run_id)
        failed = any(r["status"] == "failed" for r in results)
        store.finish(run_id, "completed_with_errors" if failed else "completed")
        return results
    except BaseException:
        if run_started:
            store.finish(run_id, "failed")
        raise
    finally:
        await close_model_clients(generator, reviewer)


async def run_batch(args: argparse.Namespace, settings: Settings) -> int:
    bundle = Bundle.load(settings, args.bundle_uri)
    body = read_bytes(args.input, settings.aws_region)
    limit = None if getattr(args, "full", False) else DEFAULT_ROW_LIMIT
    insights = parse_insights(body, limit=limit)
    # Bind checkpoints to this selection. Resuming a limited run must never
    # advance into another group of rows or silently turn into a full run.
    fingerprint_body = body if limit is None else body + f"\nMEL_LIMIT={limit}".encode()
    run_id = str(UUID(args.run_id)) if args.run_id else str(uuid4())
    store = Store(settings)
    try:
        store.initialize()
        results = await execute_run(
            bundle,
            insights,
            run_id=run_id,
            fingerprint_body=fingerprint_body,
            selection={"limit": limit},
            store=store,
            settings=settings,
            config_id=store.save_config(bundle.definition, note="Saved by mel-worker run"),
            retry_failed=args.retry_failed,
            agent_factory=build_agents,
        )
        output = "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n"
        write_bytes(args.output, output.encode(), settings.aws_region)
        counts = Counter(r["status"] for r in results)
        print(json.dumps({"run_id": run_id, "counts": counts, "output": args.output}))
        return 1 if counts["failed"] else 0
    finally:
        store.close()


async def preview(args: argparse.Namespace, settings: Settings) -> int:
    """Local artifacts and the real agent graph, without PostgreSQL, S3 or ECS."""
    if args.config is not None:
        settings = settings.model_copy(update={"config_path": args.config})
    bundle = Bundle.load(settings)
    body = args.input.read_bytes()
    rows = parse_insights(body, limit=None if args.full else DEFAULT_ROW_LIMIT)
    run_id = str(uuid4())
    directory = args.output_dir / run_id
    directory.mkdir(parents=True, exist_ok=False)
    bundle.snapshot["bedrock_project_id"] = settings.bedrock_project_id
    (directory / "bundle.json").write_text(
        json.dumps(bundle.snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "run_id": run_id,
        "mode": "local_preview",
        "config_version": bundle.config.version,
        "input": str(args.input),
        "input_sha256": hashlib.sha256(body).hexdigest(),
        "region": settings.aws_region,
        "selected_rows": len(rows),
        "selection": "full" if args.full else f"first_{DEFAULT_ROW_LIMIT}",
        "generation_model": bundle.config.generation.model_id,
        "review_model": bundle.config.review.model_id,
        "results": str(directory / "results.jsonl"),
        "status": "running",
    }
    manifest_path = directory / "run.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest), flush=True)
    outcomes = []
    generator = reviewer = None
    started = time.monotonic()
    try:
        generator, reviewer = build_agents(
            bundle.config,
            settings.aws_region,
            bundle.snapshot["prompts"],
            settings.bedrock_project_id,
        )
        deps = Dependencies.from_bundle(bundle, generator, reviewer)
        pending = iter(enumerate(rows, 1))
        with (directory / "results.jsonl").open("w", encoding="utf-8") as output:

            async def consume():
                for ordinal, insight in pending:
                    # IDs are local to this preview; handoffs are artifacts, never queued.
                    outcome = await process_insight(insight, f"S-{ordinal:010d}", deps)
                    outcomes.append(outcome)
                    output.write(outcome.model_dump_json(exclude_none=True) + "\n")
                    output.flush()
                    print(
                        json.dumps(
                            {
                                "insight_id": outcome.insight_id,
                                "status": outcome.status,
                                "attempts": outcome.attempts,
                                "relevance": outcome.review.relevance_score
                                if outcome.review
                                else None,
                                "elapsed_seconds": outcome.elapsed_seconds,
                            }
                        ),
                        flush=True,
                    )

            async with asyncio.TaskGroup() as group:
                for _ in range(bundle.config.batch.concurrency):
                    group.create_task(consume())
        counts = dict(Counter(o.status for o in outcomes))
        complete_usage = all(o.usage_complete for o in outcomes)
        summary = {
            "run_id": run_id,
            "config_version": bundle.config.version,
            "counts": counts,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "usage_complete": complete_usage,
            "input_tokens": sum(o.input_tokens for o in outcomes) if complete_usage else None,
            "output_tokens": sum(o.output_tokens for o in outcomes) if complete_usage else None,
            "results": str(directory / "results.jsonl"),
        }
        (directory / "summary.json").write_text(json.dumps(summary, indent=2))
        manifest["status"] = "completed_with_errors" if counts.get("failed") else "completed"
        print(json.dumps(summary, indent=2), flush=True)
        return 1 if counts.get("failed") else 0
    except BaseException:
        manifest["status"] = "failed"
        raise
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2))
        await close_model_clients(generator, reviewer)


def eval_rules(args: argparse.Namespace) -> None:
    """Deterministic decisions and windows vs the golden set. No model calls."""
    bundle = Bundle.load(Settings(config_path=args.config))
    report = score_decisions(bundle.decision_engine(), load_golden(args.golden)).to_dict()
    body = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("cases", "decision_accuracy", "rule_accuracy")}
    print(
        json.dumps(
            {
                **summary,
                "window": report["window"],
                "output": str(args.output) if args.output else None,
            }
        )
    )


def eval_surveys(args: argparse.Namespace) -> None:
    """Results file vs golden set: decisions, survey shape, speed. No model calls."""
    report = score_surveys(load_results(args.results), load_golden(args.golden))
    output = args.output or args.results.with_name("survey_eval.json")
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {**{k: v for k, v in report.items() if k != "side_by_side"}, "output": str(output)},
            ensure_ascii=False,
        )
    )


def golden_insights(args: argparse.Namespace) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(insights_jsonl(load_golden(args.golden)), encoding="utf-8")


def with_store(settings: Settings, operation):
    store = Store(settings)
    try:
        store.initialize()
        return operation(store)
    finally:
        store.close()


def save_config_version(args: argparse.Namespace) -> None:
    """Save the YAML configuration as a version. An identical definition returns the
    existing version; `--activate` makes it the one batch jobs use by default."""
    settings = Settings(config_path=args.config) if args.config else Settings()
    definition = Bundle.load(settings).definition

    def save(store: Store) -> dict:
        key = store.save_config(definition, parent_id=args.parent, note=args.note)
        if args.name:
            store.name_config(key, args.name, args.note)
        if args.activate:
            store.activate_config(key)
        return store.config(key)

    record = with_store(settings, save)
    print(json.dumps({k: v for k, v in record.items() if k != "definition"}))


def import_dataset(args: argparse.Namespace) -> None:
    settings = Settings()
    body = read_bytes(args.input, settings.aws_region)
    rows = parse_insights(body)
    name = args.name or Path(args.input).name
    digest = hashlib.sha256(body).hexdigest()
    print(json.dumps(with_store(settings, lambda s: s.import_dataset(name, rows, digest))))


def run_jobs(args: argparse.Namespace) -> None:
    from mel.jobs import run_queue

    settings = Settings()
    ran = with_store(
        settings,
        lambda store: asyncio.run(
            run_queue(
                store,
                settings,
                once=args.once,
                poll_seconds=args.poll_seconds,
                job_id=args.job_id,
                resume=args.resume,
            )
        ),
    )
    print(json.dumps({"event": "jobs_finished", "jobs_run": ran}))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Generate surveys and checkpoint results in PostgreSQL")
    run.add_argument("--input", required=True, help="Local JSONL path or s3://bucket/inputs/key")
    run.add_argument("--output", required=True, help="Local JSONL path or s3://bucket/outputs/key")
    run.add_argument("--run-id", help="UUID; reuse to resume the same input/config")
    run.add_argument("--bundle-uri", help="Optional local/S3 config snapshot from bundle-config")
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument(
        "--full",
        action="store_true",
        help=f"Process the entire input; default is the first {DEFAULT_ROW_LIMIT} nonblank rows",
    )
    local = sub.add_parser(
        "preview", help="Iterate locally against Bedrock; no database or deployment"
    )
    local.add_argument("--input", type=Path, default=Path("tests/fixtures/smoke_insights.jsonl"))
    local.add_argument("--config", type=Path, help="Config YAML; defaults to MEL_CONFIG_PATH")
    local.add_argument("--output-dir", type=Path, default=Path("outputs/local"))
    local.add_argument(
        "--full",
        action="store_true",
        help=f"Use the entire input; default is {DEFAULT_ROW_LIMIT} rows",
    )
    bundle = sub.add_parser("bundle-config", help="Snapshot prompts, policy, schemas and calendar")
    bundle.add_argument("--config", type=Path, default=Path("config/agent.yaml"))
    bundle.add_argument("--output", type=Path, required=True)
    sub.add_parser("check-model", help="One small Bedrock access check; no database or dataset")
    configs = sub.add_parser("config", help="Agent configuration versions in PostgreSQL")
    configs_sub = configs.add_subparsers(dest="config_command", required=True)
    save = configs_sub.add_parser("save", help="Save the YAML configuration as a version")
    save.add_argument("--config", type=Path, help="Config YAML; defaults to MEL_CONFIG_PATH")
    save.add_argument("--parent", help="config_id this version was derived from")
    save.add_argument("--note", help="What changed and why")
    save.add_argument("--name", help="Name it, making it a saved, runnable version")
    save.add_argument(
        "--activate", action="store_true", help="Make this the active version (needs --name)"
    )
    activate = configs_sub.add_parser("activate", help="Make a saved version the active one")
    activate.add_argument("config_id")
    configs_sub.add_parser("list", help="List saved versions, newest first")
    dataset = sub.add_parser("dataset", help="Insight datasets in PostgreSQL")
    dataset_sub = dataset.add_subparsers(dest="dataset_command", required=True)
    load = dataset_sub.add_parser("import", help="Import a JSONL insight file")
    load.add_argument("--input", required=True, help="Local JSONL path or s3://bucket/inputs/key")
    load.add_argument("--name", help="Display name; defaults to the file name")
    dataset_sub.add_parser("list", help="List imported datasets")
    jobs = sub.add_parser("jobs", help="Run queued jobs (batch runs and experiments)")
    jobs.add_argument("--once", action="store_true", help="Stop when the queue is empty")
    jobs.add_argument("--poll-seconds", type=float, default=2.0)
    jobs.add_argument("--job-id", help="Run only this job")
    jobs.add_argument(
        "--resume",
        action="store_true",
        help="With --job-id: also reclaim a job that stopped running; runs resume from checkpoints",
    )
    rules = sub.add_parser("eval-rules", help="Score survey decisions and windows vs golden set")
    rules.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    rules.add_argument("--config", type=Path, default=Path("config/agent.yaml"))
    rules.add_argument("--output", type=Path, help="Write the full report, mismatches included")
    surveys = sub.add_parser("eval-surveys", help="Score a results file vs golden set")
    surveys.add_argument("--results", type=Path, required=True)
    surveys.add_argument("--golden", type=Path, default=Path("eval/golden_holdout.jsonl"))
    surveys.add_argument("--output", type=Path, help="Default: survey_eval.json beside results")
    extract = sub.add_parser("golden-insights", help="Write golden insights as worker input")
    extract.add_argument("--golden", type=Path, default=Path("eval/golden_holdout.jsonl"))
    extract.add_argument("--output", type=Path, required=True)
    evaluation = sub.add_parser("eval", help="Offline evaluation; no model or database calls")
    evaluation_sub = evaluation.add_subparsers(dest="evaluation_command", required=True)
    policy = evaluation_sub.add_parser("policy", help="Score the declared held-out policy cases")
    policy.add_argument("--golden", type=Path, default=Path("eval/golden_holdout.jsonl"))
    policy.add_argument("--split", type=Path, default=Path("eval/prompt_example_split.json"))
    policy.add_argument("--config", type=Path, help="Config YAML; defaults to MEL_CONFIG_PATH")
    policy.add_argument("--output", type=Path, help="New report path; existing files are preserved")
    args = parser.parse_args()
    if args.command == "jobs" and args.resume and args.job_id is None:
        parser.error("--resume requires --job-id")
    if args.command == "config" and args.config_command == "save" and args.activate:
        if not args.name:
            parser.error("--activate requires --name")
    if args.command == "eval-rules":
        eval_rules(args)
    elif args.command == "eval-surveys":
        eval_surveys(args)
    elif args.command == "golden-insights":
        golden_insights(args)
    elif args.command == "bundle-config":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(local_bundle(args.config), indent=2, ensure_ascii=False))
    else:
        try:
            if args.command == "eval":
                from mel.evaluation import score_policy

                settings = Settings(config_path=args.config) if args.config else Settings()
                report = score_policy(
                    bundle=Bundle.load(settings),
                    golden_path=args.golden,
                    split_path=args.split,
                )
                output = args.output or Path("outputs/eval") / f"policy-{uuid4()}.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x") as target:
                    target.write(report.model_dump_json(indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            "event": "policy_evaluated",
                            "output": str(output),
                            "eligibility": report.eligibility.model_dump(),
                            "legacy_eligibility": report.legacy.eligibility.model_dump(),
                        }
                    )
                )
            elif args.command == "check-model":
                asyncio.run(check_model(Settings()))
            elif args.command == "config":
                if args.config_command == "save":
                    save_config_version(args)
                elif args.config_command == "activate":
                    with_store(Settings(), lambda s: s.activate_config(args.config_id))
                    print(json.dumps({"event": "config_activated", "config_id": args.config_id}))
                else:
                    print(json.dumps(with_store(Settings(), lambda s: s.list_configs())))
            elif args.command == "dataset":
                if args.dataset_command == "import":
                    import_dataset(args)
                else:
                    print(json.dumps(with_store(Settings(), lambda s: s.list_datasets())))
            elif args.command == "jobs":
                run_jobs(args)
            elif args.command == "preview":
                raise SystemExit(asyncio.run(preview(args, Settings())))
            else:
                raise SystemExit(asyncio.run(run_batch(args, Settings())))
        except Exception as error:
            failure = {"event": "run_failed", "error_type": type(error).__name__}
            if type(error) is ValueError:
                failure["detail"] = str(error)
            logger.error(json.dumps(failure))
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
