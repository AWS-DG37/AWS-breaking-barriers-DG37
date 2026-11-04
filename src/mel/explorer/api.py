"""HTTP boundary for configuration versions, datasets, jobs, experiments and recorded
outcomes. Long work is queued as jobs; `mel-worker jobs` executes them."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mel.bedrock_models import CatalogDiscoveryError, discover_models
from mel.bundle import Bundle
from mel.config import Settings, StrictModel
from mel.configs import active_config
from mel.datasets import InsightFilter, parse_insights
from mel.experiments import (
    ExperimentSpec,
    QualityAssessment,
    compare_runs,
    ruleset_registry,
    select_rules,
    validate_assessment,
)
from mel.explorer import auth
from mel.jobs import BatchJobSpec, dispatch
from mel.models import Outcome
from mel.store import Store

logger = logging.getLogger("mel.explorer")
PACK_INSIGHTS = Path("docs/participant-pack/data/insights.jsonl")
BROWSE_LIMIT = 500


class NewConfig(StrictModel):
    definition: dict
    parent_id: UUID | None = None
    note: str | None = None


class NewDataset(StrictModel):
    name: str
    jsonl: str


class NewBatchJob(StrictModel):
    dataset_id: UUID
    config_id: UUID | None = None  # Unset uses the active version.
    selection: InsightFilter = InsightFilter()


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
    demo_path: Path | None = None,
    live_enabled: bool = True,
    dispatcher=dispatch,
    auth_verifier=None,
    auth_exchange=None,
) -> FastAPI:
    settings = settings or Settings()
    demo_path = demo_path or Path(__file__).parent / "demo" / "recorded-run.json"
    app = FastAPI(title="FarmerChat MEL console")
    auth.install(app, settings, verifier=auth_verifier, exchange=auth_exchange)

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

    def queue(db, kind: str, spec: dict) -> dict:
        """Create a job and start its runner. A runner that cannot start fails the job,
        so nothing waits in the queue for a task that will never come."""
        job = db.create_job(str(uuid4()), kind, spec)
        try:
            dispatcher(settings, job["job_id"])
        except Exception as error:
            db.finish_job(job["job_id"], "failed", f"Dispatch failed: {type(error).__name__}")
            raise HTTPException(
                502, f"Job runner could not start ({type(error).__name__})"
            ) from None
        return job

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
            config_id, bundle = database_call(lambda db: active_config(db, settings))
            return {
                "active_config_id": config_id,
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
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                503, f"Configuration unavailable ({type(error).__name__})"
            ) from None

    @app.get("/api/configs")
    def configs():
        return {
            "configs": database_call(lambda db: db.list_configs()),
            "active_config_id": database_call(lambda db: db.active_config_id()),
        }

    @app.get("/api/configs/{config_id}")
    def config_version(config_id: UUID):
        record = database_call(lambda db: db.config(str(config_id)))
        if record is None:
            raise HTTPException(404, "Configuration version not found")
        return record

    @app.post("/api/configs", status_code=201)
    def create_config(value: NewConfig):
        try:
            definition = Bundle.from_definition(value.definition).definition
        except Exception as error:
            raise HTTPException(422, f"Invalid configuration ({type(error).__name__})") from None

        def save(db):
            if value.parent_id and db.config(str(value.parent_id)) is None:
                raise HTTPException(422, "Unknown parent configuration version")
            parent = str(value.parent_id) if value.parent_id else None
            return db.config(db.save_config(definition, parent_id=parent, note=value.note))

        return database_call(save)

    @app.post("/api/configs/{config_id}/activate")
    def activate_config(config_id: UUID):
        def activate(db):
            if db.config(str(config_id)) is None:
                raise HTTPException(404, "Configuration version not found")
            db.activate_config(str(config_id))
            return {"active_config_id": str(config_id)}

        return database_call(activate)

    @app.get("/api/datasets")
    def datasets():
        return {"datasets": database_call(lambda db: db.list_datasets())}

    @app.post("/api/datasets", status_code=201)
    def upload_dataset(value: NewDataset):
        body = value.jsonl.encode()
        return import_rows(value.name, body)

    @app.post("/api/datasets/import-pack", status_code=201)
    def import_pack():
        if not PACK_INSIGHTS.is_file():
            raise HTTPException(503, "The participant pack insights file is unavailable")
        return import_rows("Participant pack insights", PACK_INSIGHTS.read_bytes())

    def import_rows(name: str, body: bytes) -> dict:
        try:
            rows = parse_insights(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        if not rows:
            raise HTTPException(422, "The dataset contains no insights")
        digest = hashlib.sha256(body).hexdigest()
        return database_call(lambda db: db.import_dataset(name, rows, digest))

    @app.get("/api/datasets/{dataset_id}/insights")
    def dataset_insights(
        dataset_id: UUID,
        selection: Annotated[InsightFilter, Query()],
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        limit = min(selection.limit or BROWSE_LIMIT, BROWSE_LIMIT)
        bounded = selection.model_copy(update={"limit": limit})

        def query(db):
            if db.dataset(str(dataset_id)) is None:
                raise HTTPException(404, "Dataset not found")
            return db.insights(str(dataset_id), bounded, offset)

        return {"insights": database_call(query), "limit": limit, "offset": offset}

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": database_call(lambda db: db.list_jobs())}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: UUID):
        record = database_call(lambda db: db.job(str(job_id)))
        if record is None:
            raise HTTPException(404, "Job not found")
        return record

    @app.post("/api/jobs/batch", status_code=202)
    def start_batch(value: NewBatchJob):
        if not live_enabled:
            raise HTTPException(403, "Live execution is disabled")

        def enqueue(db):
            if db.dataset(str(value.dataset_id)) is None:
                raise HTTPException(422, "Unknown dataset")
            if value.config_id is None:
                config_id, _ = active_config(db, settings)
            elif db.config(str(value.config_id)) is None:
                raise HTTPException(422, "Unknown configuration version")
            else:
                config_id = str(value.config_id)
            spec = BatchJobSpec(
                dataset_id=value.dataset_id, config_id=config_id, selection=value.selection
            )
            return queue(db, "batch", spec.model_dump(mode="json"))

        return database_call(enqueue)

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

    @app.post("/api/experiments", status_code=202)
    def start_experiment(spec: ExperimentSpec):
        if not live_enabled:
            raise HTTPException(403, "Live execution is disabled")

        def enqueue(db):
            _, bundle = active_config(db, settings)
            try:
                select_rules(bundle, settings, spec.ruleset)
            except ValueError:
                raise HTTPException(422, "Unknown or invalid configured ruleset") from None
            key = str(uuid4())
            db.create_experiment(key, spec.model_dump(mode="json"))
            job = queue(db, "experiment", {"experiment_id": key})
            return {"experiment_id": key, "job_id": job["job_id"], "status": "queued"}

        return database_call(enqueue)

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
