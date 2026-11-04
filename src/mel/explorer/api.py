"""Local HTTP boundary for explicit experiments and recorded outcomes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mel.bedrock_models import CatalogDiscoveryError, discover_models
from mel.bundle import Bundle
from mel.config import Settings
from mel.experiments import (
    ExperimentSpec,
    QualityAssessment,
    compare_runs,
    execute_experiment,
    ruleset_registry,
    select_rules,
    validate_assessment,
)
from mel.models import Outcome
from mel.store import Store

logger = logging.getLogger("mel.explorer")
FLOW = {
    "nodes": [
        {"id": key, "label": label}
        for key, label in [
            ("select", "Select eligible insight"),
            ("context", "Resolve source context"),
            ("generate", "Generate survey"),
            ("schema", "Validate schema"),
            ("review", "Review survey"),
            ("decision", "Accept, revise or reject"),
            ("failure", "Execution failure"),
        ]
    ],
    "edges": [
        {"from": a, "to": b, "label": label}
        for a, b, label in [
            ("select", "context", "eligible"),
            ("context", "generate", "grounded input"),
            ("generate", "schema", "draft"),
            ("schema", "review", "valid"),
            ("schema", "generate", "revise"),
            ("review", "generate", "revise"),
            ("review", "decision", "accept or reject"),
            ("select", "decision", "skip"),
            ("generate", "failure", "provider error"),
            ("review", "failure", "provider error"),
        ]
    ],
}


def import_recorded(path: Path, store: Store) -> str:
    body = path.read_bytes()
    envelope = json.loads(body)
    if "runs" in envelope:
        spec = ExperimentSpec.model_validate(envelope["spec"])
        runs = envelope["runs"]
    else:
        bundle = envelope["bundle"]
        pairs = envelope["results"]
        spec = ExperimentSpec.model_validate(
            {
                "name": envelope["manifest"].get("name", "Recorded FarmerChat run"),
                "insights": [r["input"] for r in pairs],
                "candidates": [{"label": "Recorded run", "model": bundle["config"]["generation"]}],
                "reviewer": bundle["config"]["review"],
                "row_limit": min(50, len(pairs)),
            }
        )
        runs = [
            {"manifest": {**bundle, "recorded_manifest": envelope["manifest"]}, "results": pairs}
        ]
    if len(runs) != len(spec.candidates):
        raise ValueError("recorded runs must match the declared candidates")
    validated = []
    selected = {r["insight_id"]: r for r in spec.insights[: spec.row_limit]}
    for run in runs:
        pairs = []
        for pair in run["results"]:
            raw = pair["outcome"]
            usage = raw.get("usage_complete", "input_tokens" in raw and "output_tokens" in raw)
            outcome = Outcome.model_validate({**raw, "usage_complete": usage})
            if selected.get(outcome.insight_id) != pair["input"]:
                raise ValueError("recorded insight and outcome IDs or source records differ")
            pairs.append((pair["input"], outcome))
        if not pairs:
            raise ValueError("recorded artifact contains no outcomes")
        validated.append(pairs)
    key = str(uuid5(NAMESPACE_URL, hashlib.sha256(body).hexdigest()))
    store.create_experiment(key, spec.model_dump(mode="json"), source="recorded")
    had_errors = False
    for index, (run, pairs, candidate) in enumerate(zip(runs, validated, spec.candidates)):
        run_id = str(uuid5(NAMESPACE_URL, f"{key}/recorded/{index}"))
        snapshot = {
            **run["manifest"],
            "label": candidate.label,
            "provenance": envelope["provenance"],
        }
        store.start_run(
            run_id,
            hashlib.sha256(body).hexdigest(),
            snapshot,
            purpose="experiment",
            experiment_id=key,
        )
        done = store.completed(run_id, False)
        for insight, outcome in pairs:
            if outcome.insight_id not in done:
                store.save(run_id, insight, outcome)
        failed = any(outcome.status == "failed" for _, outcome in pairs)
        had_errors |= failed
        store.finish(run_id, "completed_with_errors" if failed else "completed")
    store.finish_experiment(key, "completed_with_errors" if had_errors else "completed")
    return key


def create_app(
    *,
    settings: Settings | None = None,
    store_factory=Store,
    catalog_loader=discover_models,
    executor=execute_experiment,
    demo_path: Path | None = None,
    live_enabled: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    demo_path = demo_path or Path(__file__).parent / "demo" / "recorded-run.json"
    jobs: set[asyncio.Task] = set()
    job_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    app = FastAPI(title="FarmerChat experiment explorer", lifespan=lifespan)

    def database():
        db = None
        try:
            db = store_factory(settings)
            db.initialize()
            return db
        except Exception as error:
            if db is not None:
                db.close()
            raise HTTPException(503, f"Database unavailable ({type(error).__name__})") from None

    def database_call(operation):
        db = database()
        try:
            return operation(db)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                503, f"Database operation unavailable ({type(error).__name__})"
            ) from None
        finally:
            db.close()

    @app.get("/api/health")
    def health():
        try:
            database_call(lambda _: None)
            return {"status": "ok", "database": "connected"}
        except HTTPException as error:
            return {"status": "ok", "database": "unavailable", "detail": error.detail}

    @app.get("/api/config")
    def configuration():
        try:
            bundle = Bundle.load(settings)
            return {
                "rulesets": [
                    {"id": row["id"], "label": row["label"]} for row in ruleset_registry(settings)
                ],
                "default_reviewer": bundle.config.review.model_dump(mode="json"),
                "default_candidates": [
                    {
                        "label": "Configured model",
                        "model": bundle.config.generation.model_dump(mode="json"),
                    }
                ],
                "flow": FLOW,
                "live_enabled": live_enabled,
            }
        except Exception as error:
            raise HTTPException(
                503, f"Configuration unavailable ({type(error).__name__})"
            ) from None

    @app.get("/api/models")
    async def models():
        try:
            candidates = await catalog_loader(settings.aws_region)
            return {"models": [m.model_dump(mode="json") for m in candidates]}
        except CatalogDiscoveryError as error:
            return {
                "models": [m.model_dump(mode="json") for m in error.models],
                "error": (
                    "Some model catalogs are unavailable. Discovered entries remain unverified."
                ),
            }
        except Exception as error:
            return {"models": [], "error": f"Model catalog unavailable ({type(error).__name__})"}

    @app.get("/api/experiments")
    def experiments():
        return {"experiments": database_call(lambda db: db.list_experiments())}

    @app.get("/api/experiments/{experiment_id}")
    def experiment(
        experiment_id: UUID, evaluation_reference: Literal["rules", "golden"] | None = None
    ):
        detail = database_call(lambda db: db.experiment_detail(str(experiment_id)))
        if detail is None:
            raise HTTPException(404, "Experiment not found")
        return {
            **detail,
            "comparison": compare_runs(detail, reference=evaluation_reference),
            "flow": FLOW,
        }

    async def execute(spec, key):
        db = None
        try:
            db = await asyncio.to_thread(database)
            await executor(spec, settings=settings, store=db, experiment_id=key)
        except BaseException as error:
            logger.error("Experiment %s failed (%s)", key, type(error).__name__)
            if db is not None:
                await asyncio.to_thread(db.finish_experiment, key, "failed")
        finally:
            if db is not None:
                await asyncio.to_thread(db.close)

    @app.post("/api/experiments", status_code=202)
    async def start_experiment(spec: ExperimentSpec):
        if not live_enabled:
            raise HTTPException(403, "Live execution is disabled")
        try:
            select_rules(Bundle.load(settings), settings, spec.ruleset)
        except ValueError:
            raise HTTPException(422, "Unknown or invalid configured ruleset") from None
        except Exception as error:
            raise HTTPException(
                503, f"Configuration unavailable ({type(error).__name__})"
            ) from None
        async with job_lock:
            if len(jobs) >= 2:
                raise HTTPException(429, "Two experiments are already active. Wait for completion.")
            key = str(uuid4())
            await asyncio.to_thread(
                database_call, lambda db: db.create_experiment(key, spec.model_dump(mode="json"))
            )
            task = asyncio.create_task(execute(spec, key))
            jobs.add(task)
            task.add_done_callback(jobs.discard)
        return {"experiment_id": key, "status": "queued"}

    @app.post("/api/experiments/{experiment_id}/assessments")
    def assessment(experiment_id: UUID, value: QualityAssessment):
        if value.assessor != "human":
            raise HTTPException(
                422, "This endpoint records explicitly supplied human assessments only"
            )

        def save(db):
            detail = db.experiment_detail(str(experiment_id))
            if detail is None:
                raise HTTPException(404, "Experiment not found")
            try:
                validate_assessment(detail, value)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            return db.save_assessment(str(experiment_id), value.model_dump(mode="json"))

        return database_call(save)

    @app.post("/api/import-recorded")
    def recorded():
        if not demo_path.is_file():
            raise HTTPException(503, "Configured recorded artifact is unavailable")
        return {"experiment_id": database_call(lambda db: import_recorded(demo_path, db))}

    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="explorer-assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(static / "index.html")

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-live", action="store_true", help="Disable live model execution")
    args = parser.parse_args()
    uvicorn.run(create_app(live_enabled=not args.no_live), host=args.host, port=args.port)
