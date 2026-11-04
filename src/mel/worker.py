"""Batch CLI. Container startup does no work until an explicit command is supplied."""

import argparse
import asyncio
import hashlib
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


async def process_insight(insight: dict, survey_id: str, deps: Dependencies) -> Outcome:
    """The same generation/review loop for both local preview and persisted batches."""
    started = time.monotonic()
    state = State(insight, survey_id, datetime.now(IST))
    try:
        async with asyncio.timeout(deps.config.graph.insight_timeout_seconds):
            result = await survey_graph.run(Select(), state=state, deps=deps)
        outcome = result.output
    except Exception as error:
        outcome = Outcome(
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
        outcome.usage_complete
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


def parse_insights(body: bytes, limit: int | None = None) -> list[dict]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    rows, seen = [], set()
    for line_number, line in enumerate(body.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"line {line_number} is not an object")
        key = row.get("insight_id")
        if not isinstance(key, str) or not key:
            # Preserve malformed rows for rejection instead of crashing the batch.
            row = {**row, "insight_id": f"invalid-row-{line_number}", "quality_flag": "junk"}
        if row["insight_id"] in seen:
            raise ValueError(f"duplicate insight_id on line {line_number}")
        seen.add(row["insight_id"])
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


async def run_batch(args: argparse.Namespace, settings: Settings) -> int:
    bundle = Bundle.load(settings, args.bundle_uri)
    config = bundle.config
    body = read_bytes(args.input, settings.aws_region)
    limit = None if getattr(args, "full", False) else DEFAULT_ROW_LIMIT
    insights = parse_insights(body, limit=limit)
    # Bind checkpoints to this selection. Resuming a limited run must never
    # advance into another group of rows or silently turn into a full run.
    fingerprint_body = body if limit is None else body + f"\nMEL_LIMIT={limit}".encode()
    run_id = str(UUID(args.run_id)) if args.run_id else str(uuid4())
    generator, reviewer = build_agents(
        config, settings.aws_region, bundle.snapshot["prompts"], settings.bedrock_project_id
    )
    deps = Dependencies.from_bundle(bundle, generator, reviewer)
    store = None
    try:
        store = Store(settings)
        store.initialize()
        bundle.snapshot["bedrock_project_id"] = settings.bedrock_project_id
        store.start_run(
            run_id,
            bundle.fingerprint(fingerprint_body, settings.aws_region),
            {**bundle.snapshot, "selection": {"limit": limit, "selected_rows": len(insights)}},
        )
        logger.info(
            json.dumps(
                {
                    "event": "run_started",
                    "run_id": run_id,
                    "limit": limit,
                    "selected_rows": len(insights),
                }
            )
        )
        done = store.completed(run_id, args.retry_failed)
        pending = iter(row for row in insights if row["insight_id"] not in done)

        async def consume() -> None:
            # Exactly concurrency workers; do not create thousands of waiting tasks.
            for insight in pending:
                outcome = await process_insight(
                    insight, await asyncio.to_thread(store.next_survey_id), deps
                )
                # Persistence failures abort the task. Never pretend uncheckpointed work succeeded.
                await asyncio.to_thread(store.save, run_id, insight, outcome)
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
        output = "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n"
        write_bytes(args.output, output.encode(), settings.aws_region)
        counts = Counter(r["status"] for r in results)
        store.finish(run_id, "completed_with_errors" if counts["failed"] else "completed")
        print(json.dumps({"run_id": run_id, "counts": counts, "output": args.output}))
        return 1 if counts["failed"] else 0
    finally:
        if store is not None:
            store.close()
        client = getattr(getattr(generator, "model", None), "client", None)
        if client is not None:
            await client.close()


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
    generator = None
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
        client = getattr(getattr(generator, "model", None), "client", None)
        if client is not None:
            await client.close()


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
    args = parser.parse_args()
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
            if args.command == "check-model":
                asyncio.run(check_model(Settings()))
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
