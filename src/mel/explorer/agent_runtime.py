"""Console routes for the Strands survey agents deployed on Bedrock AgentCore Runtime.

GET  /api/agents/options       models and pipelines the deployed agent accepts
GET  /api/agents/insights      golden insights to try, with the expected rule
POST /api/agents/runs          start one insight on one model; returns {"run_id"} at once
GET  /api/agents/runs/{id}     {"status": "running" | "done" | "failed", ...}
GET  /api/agents/comparison    summaries of saved model sweeps (agents/sweep.py run files)

A survey takes 15-60 seconds and the hosted API (API Gateway + Lambda) answers within 29, so a
run is started in the background and the page polls for it:
  hosted   the console Lambda invokes itself asynchronously (InvocationType=Event); the Lambda Web
           Adapter delivers that event to POST /events, which calls AgentCore and saves the
           result as JSON under MEL_AGENT_RESULTS_URI (s3://...). /events is not routed by
           API Gateway, which only forwards /api/*.
  local    a thread calls AgentCore and keeps the result in memory.

The agent validates models against its own allowlist; the console passes choices through.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field

from mel.config import Settings, StrictModel

logger = logging.getLogger("mel.explorer.agents")
GOLDEN_FILES = (Path("eval/golden_dev.jsonl"), Path("eval/golden_holdout.jsonl"))
RUN_EVENT = "mel_agent_run"


class AgentRun(StrictModel):
    insight_id: str = Field(min_length=1, max_length=40)
    generator: str | None = Field(default=None, max_length=120)
    verifier: str | None = Field(default=None, max_length=120)
    pipeline: Literal["direct", "translate"] = "direct"
    max_retries: int = Field(default=1, ge=0, le=2)


class AgentRuntime:
    """Calls the deployed agent. One fresh runtime session per call: requests are independent."""

    def __init__(self, arn: str, region: str):
        import boto3
        from botocore.config import Config

        self.arn = arn
        # A survey with retries can take over a minute; the default 60 s read timeout is too short.
        self.client = boto3.client(
            "bedrock-agentcore",
            region_name=region,
            config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 1}),
        )

    def __call__(self, payload: dict) -> dict:
        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.arn,
            runtimeSessionId=f"mel-console-{uuid.uuid4()}",  # must be at least 33 characters
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
        return json.loads(response["response"].read())


class MemoryResults:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put(self, run_id: str, record: dict) -> None:
        self.items[run_id] = record

    def get(self, run_id: str) -> dict | None:
        return self.items.get(run_id)


class S3Results:
    """One JSON object per run under s3://bucket/prefix/<run id>.json."""

    def __init__(self, uri: str, region: str):
        import boto3

        bucket, _, prefix = uri.removeprefix("s3://").partition("/")
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.s3 = boto3.client("s3", region_name=region)

    def _key(self, run_id: str) -> str:
        return f"{self.prefix}/{run_id}.json" if self.prefix else f"{run_id}.json"

    def put(self, run_id: str, record: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._key(run_id),
            Body=json.dumps(record, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    def get(self, run_id: str) -> dict | None:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self._key(run_id))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None
        return json.loads(body)


def _read(uri: str, region: str) -> bytes | None:
    if uri.startswith("s3://"):
        import boto3

        bucket, _, key = uri.removeprefix("s3://").partition("/")
        s3 = boto3.client("s3", region_name=region)
        try:
            return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except s3.exceptions.NoSuchKey:
            return None
    path = Path(uri)
    return path.read_bytes() if path.is_file() else None


def _golden(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


@lru_cache(maxsize=1)
def golden_insights() -> dict[str, dict]:
    rows = {}
    for path in GOLDEN_FILES:
        for row in _golden(path):
            rows[row["insight"]["insight_id"]] = {**row, "split": path.stem.removeprefix("golden_")}
    return rows


def sweep_summaries(runs_dir: Path) -> list[dict]:
    """One row per saved sweep run, newest first. Unreadable files are skipped."""
    if not runs_dir.is_dir():
        return []
    rows = []
    for path in sorted(runs_dir.rglob("*.json"), reverse=True):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            summary = run["summary"]
        except (OSError, ValueError, KeyError):
            continue
        tokens = summary.get("tokens") or {}
        rows.append(
            {
                "file": str(path.relative_to(runs_dir)),
                "created_at": run.get("created_at"),
                "model_id": run.get("model_id"),
                "pipeline": run.get("pipeline") or "direct",
                "tag": run.get("tag"),
                "prompt_sha": run.get("prompt_sha"),
                "golden": run.get("golden"),
                "verifier_model_id": run.get("verifier_model_id"),
                "cases": summary.get("cases"),
                "status_counts": summary.get("status_counts"),
                "decision_accuracy": summary.get("decision_accuracy"),
                "accept_rate": summary.get("accept_rate"),
                "relevance_mean": summary.get("relevance_mean"),
                "attempts_mean": summary.get("attempts_mean"),
                "tokens_per_survey": tokens.get("per_survey") if isinstance(tokens, dict) else None,
                "seconds_per_survey": summary.get("seconds_per_survey_mean"),
            }
        )
    return rows


def install(app: FastAPI, settings: Settings, *, invoker=None, results=None, dispatch=None) -> None:
    runtime = invoker
    if runtime is None and settings.agent_runtime_arn:
        runtime = AgentRuntime(settings.agent_runtime_arn, settings.aws_region)
    if results is None:
        results = (
            S3Results(settings.agent_results_uri, settings.aws_region)
            if settings.agent_results_uri
            else MemoryResults()
        )
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")

    def call(payload: dict) -> dict:
        """Returns the agent's result, or raises HTTPException with a message for the page."""
        if runtime is None:
            raise HTTPException(
                503, "The survey agent is not configured (set MEL_AGENT_RUNTIME_ARN)"
            )
        try:
            reply = runtime(payload)
        except Exception as error:
            logger.exception("AgentCore invocation failed")
            raise HTTPException(
                502, f"The survey agent did not answer ({type(error).__name__})"
            ) from None
        if not reply.get("ok"):
            raise HTTPException(422, reply.get("error") or "The survey agent refused the request")
        return reply["result"]

    def execute(run_id: str, value: AgentRun) -> None:
        """Run one survey and save the outcome. Never raises: the page reads the saved status."""
        row = golden_insights()[value.insight_id]
        started = time.time()
        try:
            result = call(
                {
                    "insight": row["insight"],
                    "generator": value.generator,
                    "verifier": value.verifier,
                    "pipeline": value.pipeline,
                    "max_retries": value.max_retries,
                }
            )
            record = {"status": "done", "result": result, "expected": row.get("expected")}
        except HTTPException as error:
            record = {"status": "failed", "error": error.detail}
        except Exception as error:  # a broken run must still leave a status behind
            logger.exception("Agent run %s failed", run_id)
            record = {"status": "failed", "error": f"Run failed ({type(error).__name__})"}
        record.update(
            run_id=run_id, request=value.model_dump(), seconds=round(time.time() - started, 1)
        )
        results.put(run_id, record)

    def start_in_background(run_id: str, value: AgentRun) -> None:
        if dispatch is not None:
            dispatch(run_id, value, execute)
        elif function_name:
            import boto3

            boto3.client("lambda", region_name=settings.aws_region).invoke(
                FunctionName=function_name,
                InvocationType="Event",
                Payload=json.dumps({RUN_EVENT: {"run_id": run_id, **value.model_dump()}}),
            )
        else:
            threading.Thread(target=execute, args=(run_id, value), daemon=True).start()

    @app.get("/api/agents/options")
    def agent_options():
        if runtime is None:
            return {"configured": False}
        return {"configured": True, **call({"action": "options"})}

    @app.get("/api/agents/insights")
    def agent_insights():
        return {
            "insights": [
                {
                    "insight_id": insight_id,
                    "golden_id": row.get("golden_id"),
                    "split": row["split"],
                    "language": row["insight"].get("conversation_language"),
                    "crop": (row["insight"].get("asset") or {}).get("name"),
                    "resolution_type": row["insight"].get("resolution_type"),
                    "query_summary": row["insight"].get("query_summary"),
                    "expected": row.get("expected"),
                }
                for insight_id, row in golden_insights().items()
            ]
        }

    @app.get("/api/agents/insights/{insight_id}")
    def agent_insight(insight_id: str):
        row = golden_insights().get(insight_id)
        if row is None:
            raise HTTPException(404, "Unknown insight")
        return {"insight": row["insight"], "expected": row.get("expected")}

    # The rules gate alone (code, no model calls). Answers in a second or two, so it runs
    # inside the API's 29 s limit; the Demo tab shows it before the survey is written.
    @app.post("/api/agents/decide")
    def agent_decide(value: AgentRun):
        row = golden_insights().get(value.insight_id)
        if row is None:
            raise HTTPException(404, "Unknown insight")
        return {"decision": call({"action": "decide", "insight": row["insight"]})}

    @app.post("/api/agents/runs", status_code=202)
    def agent_run(value: AgentRun):
        if runtime is None:
            raise HTTPException(503, "The survey agent is not configured")
        if value.insight_id not in golden_insights():
            raise HTTPException(404, "Unknown insight")
        run_id = str(uuid.uuid4())
        results.put(run_id, {"status": "running", "run_id": run_id, "request": value.model_dump()})
        try:
            start_in_background(run_id, value)
        except Exception as error:
            logger.exception("Could not start agent run")
            raise HTTPException(502, f"Could not start the run ({type(error).__name__})") from None
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/agents/runs/{run_id}")
    def agent_run_status(run_id: uuid.UUID):
        record = results.get(str(run_id))
        if record is None:
            raise HTTPException(404, "Unknown run")
        return record

    # Lambda Web Adapter posts non-HTTP events (our async self-invocation) here. API Gateway
    # forwards only /api/*, so this path is not reachable from the internet.
    @app.post("/events", include_in_schema=False)
    async def lambda_event(request: Request):
        event = await request.json()
        job = event.get(RUN_EVENT) if isinstance(event, dict) else None
        if not job or not function_name:
            raise HTTPException(404)
        run_id = job.pop("run_id")
        execute(run_id, AgentRun(**job))
        return {"ok": True}

    @app.get("/api/agents/comparison")
    def agent_comparison():
        # Hosted: deploy.sh uploads a precomputed summary file. Local: read the run files.
        if settings.agent_summaries_uri:
            body = _read(settings.agent_summaries_uri, settings.aws_region)
            return {"runs": json.loads(body)["runs"] if body else []}
        return {"runs": sweep_summaries(settings.agent_runs_dir)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Write sweep summaries for the hosted console")
    parser.add_argument("runs_dir", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    rows = sweep_summaries(args.runs_dir)
    args.out.write_text(json.dumps({"runs": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rows)} sweep runs -> {args.out}")


if __name__ == "__main__":
    main()
