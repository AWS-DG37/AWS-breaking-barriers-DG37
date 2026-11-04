"""HTTP boundary for configuration versions, datasets, jobs, experiments and recorded
outcomes. Long work is queued as jobs; `mel-worker jobs` executes them."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError, model_validator

from mel.bedrock_models import CatalogDiscoveryError, discover_models
from mel.bundle import Bundle, read_bytes
from mel.config import Settings, StrictModel
from mel.configs import active_config, config_bundle, validate_definition
from mel.costs import (
    CostRefused,
    NewPrice,
    check_budget_prices,
    estimate_run,
    prices,
    report,
    save_price,
)
from mel.datasets import InsightFilter, parse_insights
from mel.evaluation import load_golden, score_decisions
from mel.experiments import (
    ExperimentCandidate,
    ExperimentSpec,
    QualityAssessment,
    compare_runs,
    validate_assessment,
)
from mel.explorer import auth
from mel.jobs import BatchJobSpec, dispatch, estimate_batch
from mel.models import Outcome
from mel.store import Store

logger = logging.getLogger("mel.explorer")
GOLDEN_HOLDOUT = Path("eval/golden_holdout.jsonl")
BROWSE_LIMIT = 500


class NewConfig(StrictModel):
    definition: dict
    parent_id: UUID | None = None
    note: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)
    save: bool = False  # True: a saved, runnable version (needs a name); False: a draft.

    @model_validator(mode="after")
    def saved_versions_are_named(self):
        if self.save and not self.name:
            raise ValueError("name the version to save it")
        return self


class ConfigName(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"\S")
    note: str | None = Field(default=None, max_length=500)


class NewExperimentCandidate(StrictModel):
    label: str = Field(min_length=1, max_length=120, pattern=r"\S")
    config_id: UUID


class NewExperiment(StrictModel):
    """Compare saved versions on the same insights, chosen like a Generate selection."""

    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    candidates: list[NewExperimentCandidate] = Field(min_length=1, max_length=6)
    dataset_id: UUID
    selection: InsightFilter = InsightFilter(limit=10)
    evaluation_reference: Literal["rules", "golden"] = "rules"
    budget_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)


class NewDataset(StrictModel):
    name: str
    jsonl: str


class NewBatchJob(StrictModel):
    budget_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    dataset_id: UUID
    config_id: UUID | None = None  # Unset uses the active version.
    selection: InsightFilter = InsightFilter()


FLOW = {
    "nodes": [
        {"id": key, "label": label}
        for key, label in [
            ("context", "Source context (optional)"),
            ("select", "Eligibility and timing"),
            ("generate", "Generate survey"),
            ("schema", "Validate schema"),
            ("review", "Review survey"),
            ("decision", "Accepted, skipped or rejected"),
            ("failure", "Execution failure"),
        ]
    ],
    "edges": [
        {"from": a, "to": b, "label": label}
        for a, b, label in [
            ("context", "select", "when supplied"),
            ("select", "generate", "eligible"),
            ("generate", "schema", "draft"),
            ("schema", "review", "valid"),
            ("schema", "generate", "invalid; revisions remain"),
            ("schema", "decision", "invalid; revisions exhausted"),
            ("review", "generate", "failed gates; revisions remain"),
            ("review", "decision", "accept or reject"),
            ("select", "decision", "skip"),
            ("select", "failure", "execution error"),
            ("schema", "failure", "execution error"),
            ("generate", "failure", "execution error"),
            ("review", "failure", "execution or contract error"),
        ]
    ],
}


def recorded_spec(name: str, insights: list[dict], labels: list[str]) -> ExperimentSpec:
    """Recorded artifacts predate configuration versions; their candidates are labels,
    and each run's manifest holds the configuration it used."""
    return ExperimentSpec(
        name=name,
        insights=insights,
        candidates=[ExperimentCandidate(label=label) for label in labels],
    )


def import_recorded(path: Path, store: Store) -> str:
    body = path.read_bytes()
    envelope = json.loads(body)
    if "runs" in envelope:
        recorded = envelope["spec"]
        spec = recorded_spec(
            recorded["name"],
            recorded["insights"][: recorded.get("row_limit", len(recorded["insights"]))],
            [c["label"] for c in recorded["candidates"]],
        )
        runs = envelope["runs"]
    else:
        bundle = envelope["bundle"]
        pairs = envelope["results"]
        spec = recorded_spec(
            envelope["manifest"].get("name", "Recorded FarmerChat run"),
            [r["input"] for r in pairs][:50],
            ["Recorded run"],
        )
        runs = [
            {"manifest": {**bundle, "recorded_manifest": envelope["manifest"]}, "results": pairs}
        ]
    if len(runs) != len(spec.candidates):
        raise ValueError("recorded runs must match the declared candidates")
    validated = []
    selected = {r["insight_id"]: r for r in spec.insights}
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

    @app.exception_handler(CostRefused)
    async def costs_refused(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/prices")
    def model_prices():
        return {"prices": database_call(prices)}

    @app.post("/api/prices", status_code=201)
    def add_price(value: NewPrice):
        return database_call(lambda db: save_price(db, value))

    @app.get("/api/runs/{run_id}/costs")
    def run_costs(
        run_id: UUID,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return database_call(lambda db: report(db, str(run_id), limit=limit, offset=offset))

    schema_ready = False
    schema_lock = Lock()

    def database():
        nonlocal schema_ready
        db = None
        try:
            db = store_factory(settings)
            # Migrations take a PostgreSQL advisory lock. Running them on every
            # request serialized the parallel calls made by each console screen.
            if not schema_ready:
                with schema_lock:
                    if not schema_ready:
                        db.initialize()
                        schema_ready = True
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
        except (HTTPException, CostRefused):
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
            config_id, _ = database_call(lambda db: active_config(db, settings))
            return {
                "active_config_id": config_id,
                "flow": FLOW,
                "live_enabled": live_enabled,
                "allowed_models": [m.model_dump() for m in settings.allowed_models],
            }
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                503, f"Configuration unavailable ({type(error).__name__})"
            ) from None

    @app.get("/api/configs")
    def configs():
        return database_call(
            lambda db: {"configs": db.list_configs(), "active_config_id": db.active_config_id()}
        )

    @app.get("/api/configs/{config_id}")
    def config_version(config_id: UUID):
        record = database_call(lambda db: db.config(str(config_id)))
        if record is None:
            raise HTTPException(404, "Configuration version not found")
        return record

    @app.post("/api/configs", status_code=201)
    def create_config(value: NewConfig):
        """Store a definition as a draft or, with `save`, a saved version. An identical
        definition returns the version that already holds it."""
        definition = runnable(value.definition).definition

        def save(db):
            if value.parent_id and db.config(str(value.parent_id)) is None:
                raise HTTPException(422, "Unknown parent configuration version")
            parent = str(value.parent_id) if value.parent_id else None
            key = db.save_config(definition, parent_id=parent, note=value.note, name=value.name)
            if value.save:
                db.name_config(key, value.name, value.note)
            return db.config(key)

        return database_call(save)

    def runnable(definition: dict) -> Bundle:
        try:
            return validate_definition(definition, settings)
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise HTTPException(422, f"Invalid configuration: {where}: {first['msg']}") from None
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(422, f"Invalid configuration: {error}") from None

    @app.post("/api/configs/{config_id}/save")
    def name_config(config_id: UUID, value: ConfigName):
        def save(db):
            if db.config(str(config_id)) is None:
                raise HTTPException(404, "Configuration version not found")
            db.name_config(str(config_id), value.name.strip(), value.note)
            return db.config(str(config_id))

        return database_call(save)

    @app.get("/api/configs/{config_id}/rules-check")
    def rules_check(config_id: UUID):
        """The version's decision rules and send windows against the golden holdout:
        free and instant, because no model is called."""

        def check(db):
            if db.config(str(config_id)) is None:
                raise HTTPException(404, "Configuration version not found")
            engine = config_bundle(db, str(config_id)).decision_engine()
            report = score_decisions(engine, load_golden(GOLDEN_HOLDOUT)).to_dict()
            return {
                key: report[key]
                for key in ("cases", "decision_accuracy", "rule_accuracy", "window")
            } | {"mismatches": report["mismatches"][:20]}

        return database_call(check)

    @app.post("/api/configs/{config_id}/activate")
    def activate_config(config_id: UUID):
        def activate(db):
            if db.config(str(config_id)) is None:
                raise HTTPException(404, "Configuration version not found")
            try:
                db.activate_config(str(config_id))
            except ValueError as error:
                raise HTTPException(422, str(error).capitalize()) from None
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
        try:
            body = read_bytes(settings.pack_uri, settings.aws_region)
        except Exception as error:
            raise HTTPException(
                503, f"Participant pack unavailable ({type(error).__name__})"
            ) from None
        return import_rows("Participant pack", body)

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

    @app.get("/api/datasets/{dataset_id}/facets")
    def dataset_facets(dataset_id: UUID):
        def facets(db):
            dataset = db.dataset(str(dataset_id))
            if dataset is None:
                raise HTTPException(404, "Dataset not found")
            return {**dataset, "facets": db.dataset_facets(str(dataset_id))}

        return database_call(facets)

    @app.get("/api/datasets/{dataset_id}/count")
    def dataset_count(dataset_id: UUID, selection: Annotated[InsightFilter, Query()]):
        def count(db):
            if db.dataset(str(dataset_id)) is None:
                raise HTTPException(404, "Dataset not found")
            unlimited = selection.model_copy(update={"limit": None})
            matched = db.count_insights(str(dataset_id), unlimited)
            return {"matched": matched, "selected": min(matched, selection.limit or matched)}

        return database_call(count)

    def batch_spec(db, value: NewBatchJob) -> BatchJobSpec:
        if db.dataset(str(value.dataset_id)) is None:
            raise HTTPException(422, "Unknown dataset")
        if value.config_id is None:
            config_id, _ = active_config(db, settings)
        elif db.config(str(value.config_id)) is None:
            raise HTTPException(422, "Unknown configuration version")
        else:
            config_id = str(value.config_id)
        return BatchJobSpec(
            dataset_id=value.dataset_id,
            config_id=config_id,
            selection=value.selection,
            budget_usd=value.budget_usd,
        )

    @app.post("/api/jobs/batch/estimate")
    def estimate(value: NewBatchJob):
        """The rules' verdict on the selection and the rough model usage, before any spend."""

        def run(db):
            spec = batch_spec(db, value)
            bundle = config_bundle(db, str(spec.config_id))
            insights = db.insights(str(spec.dataset_id), spec.selection)
            result = {"config_id": str(spec.config_id), **estimate_batch(bundle, insights)}
            if hasattr(db, "connection"):
                result["costs"] = estimate_run(
                    db, bundle.config, settings.aws_region, result["surveys"]
                )
                amount = result["costs"]["estimated_usd"]
                result["cost_usd"] = float(amount) if amount is not None else None
            return result

        return database_call(run)

    @app.get("/api/runs")
    def runs():
        return {"runs": database_call(lambda db: db.list_runs())}

    @app.get("/api/runs/{run_id}")
    def run(run_id: UUID):
        summary = database_call(lambda db: db.run_summary(str(run_id)))
        if summary is None:
            raise HTTPException(404, "Run not found")
        summary["costs"] = database_call(lambda db: report(db, str(run_id)))
        return summary

    @app.get("/api/runs/{run_id}/results")
    def run_results(
        run_id: UUID,
        status: Literal["accepted", "skipped", "rejected", "failed"] | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        rows = database_call(lambda db: db.run_results(str(run_id), status, offset, limit))
        return {"results": rows, "offset": offset, "limit": limit}

    @app.get("/api/runs/{run_id}/results/{insight_id}")
    def run_result(run_id: UUID, insight_id: str):
        record = database_call(lambda db: db.run_result(str(run_id), insight_id))
        if record is None:
            raise HTTPException(404, "Result not found")
        return record

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
            spec = batch_spec(db, value)
            if hasattr(db, "connection") and spec.budget_usd is not None:
                bundle = config_bundle(db, str(spec.config_id))
                insights = db.insights(str(spec.dataset_id), spec.selection)
                if estimate_batch(bundle, insights)["surveys"]:
                    check_budget_prices(db, bundle.config, settings.aws_region, spec.budget_usd)
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

        def costs(db):
            if hasattr(db, "connection"):
                for run in detail["runs"]:
                    run["costs"] = report(db, run["run_id"])

        database_call(costs)
        return {
            **detail,
            "comparison": compare_runs(detail, reference=evaluation_reference),
            "flow": FLOW,
        }

    @app.post("/api/experiments/estimate")
    def experiment_estimate(value: NewExperiment):
        def estimate(db):
            if db.dataset(str(value.dataset_id)) is None:
                raise HTTPException(422, "Unknown dataset")
            insights = db.insights(str(value.dataset_id), value.selection)
            entries = []
            for candidate in value.candidates:
                if db.config(str(candidate.config_id)) is None:
                    raise HTTPException(422, "Unknown configuration version")
                bundle = config_bundle(db, str(candidate.config_id))
                surveys = estimate_batch(bundle, insights)["surveys"]
                entries.append(
                    {
                        "label": candidate.label,
                        **estimate_run(db, bundle.config, settings.aws_region, surveys),
                    }
                )
            return {"candidates": entries, "budget_scope": "Each candidate run has its own budget"}

        return database_call(estimate)

    @app.post("/api/experiments", status_code=202)
    def start_experiment(value: NewExperiment):
        if not live_enabled:
            raise HTTPException(403, "Live execution is disabled")
        if (value.selection.limit or 51) > 50:
            raise HTTPException(422, "An experiment compares at most 50 insights")

        def enqueue(db):
            if db.dataset(str(value.dataset_id)) is None:
                raise HTTPException(422, "Unknown dataset")
            for candidate in value.candidates:
                record = db.config(str(candidate.config_id))
                if record is None:
                    raise HTTPException(422, f"Unknown configuration version for {candidate.label}")
                runnable(record["definition"])
            insights = db.insights(str(value.dataset_id), value.selection)
            if not insights:
                raise HTTPException(422, "No insights match this selection")
            if hasattr(db, "connection") and value.budget_usd is not None:
                for candidate in value.candidates:
                    bundle = config_bundle(db, str(candidate.config_id))
                    if estimate_batch(bundle, insights)["surveys"]:
                        check_budget_prices(
                            db, bundle.config, settings.aws_region, value.budget_usd
                        )
            try:
                spec = ExperimentSpec(
                    name=value.name,
                    insights=insights,
                    candidates=[c.model_dump() for c in value.candidates],
                    evaluation_reference=value.evaluation_reference,
                    budget_usd=value.budget_usd,
                )
            except ValidationError as error:
                raise HTTPException(422, error.errors()[0]["msg"]) from None
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
