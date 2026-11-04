"""Configuration versions, datasets and the job queue against real PostgreSQL.

Rollback-only, like test_postgres_integration. Models are Pydantic AI TestModels.
"""

import asyncio
import hashlib
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from postgres import requires_postgres
from test_trace import dependencies

from mel.bundle import Bundle
from mel.config import Settings
from mel.datasets import InsightFilter, parse_insights
from mel.experiments import ExperimentSpec
from mel.jobs import BatchJobSpec, run_queue

pytestmark = requires_postgres
SETTINGS = Settings(bedrock_model_id="test")
SMOKE = Path("tests/fixtures/smoke_insights.jsonl")


def agents(*args):
    deps = dependencies()
    return deps.generator, deps.reviewer


def smoke_dataset(db):
    body = SMOKE.read_bytes()
    # A unique digest per test keeps rollback-only tests independent of earlier imports.
    digest = hashlib.sha256(body + uuid4().bytes).hexdigest()
    return db.import_dataset("Smoke", parse_insights(body), digest)


def active(db):
    key = db.save_config(Bundle.load(SETTINGS).definition, note="test")
    db.activate_config(key)
    return key


def test_identical_definitions_share_one_version_and_one_version_is_active(database):
    definition = Bundle.load(SETTINGS).definition
    first = database.save_config(definition, note="first")
    assert database.save_config(definition, note="again") == first
    changed = {**definition, "prompts": {**definition["prompts"], "generate": "Changed."}}
    second = database.save_config(changed, parent_id=first, note="prompt edit")
    database.activate_config(first)
    database.activate_config(second)
    assert database.active_config_id() == second
    assert database.config(first)["status"] == "retired"
    assert database.config(second)["parent_id"] == first
    with pytest.raises(ValueError, match="unknown"):
        database.activate_config(str(uuid4()))


def test_dataset_import_is_idempotent_and_filters_by_column_and_ist_day(database):
    dataset = smoke_dataset(database)
    assert dataset["created"] and dataset["row_count"] == 10
    body = SMOKE.read_bytes()
    assert database.import_dataset("Other name", parse_insights(body), "x" * 64)["created"]
    assert not database.import_dataset("Again", parse_insights(body), "x" * 64)["created"]
    key = dataset["dataset_id"]
    wheat = database.insights(key, InsightFilter(crop="wheat"))
    assert {row["insight_id"] for row in wheat} == {
        "I-004871",
        "I-004420",
        "I-004101",
        "I-000709",
        "I-003405",
    }
    december = database.insights(
        key, InsightFilter(created_from=date(2026, 12, 1), created_to=date(2026, 12, 31))
    )
    assert {row["insight_id"] for row in december} == {
        "I-004512",
        "I-004871",
        "I-004990",
        "I-004420",
    }
    first_two = database.insights(key, InsightFilter(limit=2))
    assert [row["insight_id"] for row in first_two] == ["I-004512", "I-004871"]
    assert database.insights(key, InsightFilter(limit=2), offset=9)[0]["insight_id"] == "I-003405"


def test_batch_job_runs_the_selection_with_its_version_and_reports_progress(database):
    config_id = active(database)
    dataset = smoke_dataset(database)
    spec = BatchJobSpec(
        dataset_id=dataset["dataset_id"],
        config_id=config_id,
        selection=InsightFilter(crop="wheat", limit=3),
    )
    job = database.create_job(str(uuid4()), "batch", spec.model_dump(mode="json"))
    ran = asyncio.run(run_queue(database, SETTINGS, job_id=job["job_id"], agent_factory=agents))
    record = database.job(job["job_id"])
    assert ran == 1
    assert record["status"] == "completed", record["error"]
    assert record["progress"] == {"done": 3, "total": 3}
    (run,) = record["runs"]
    assert run["config_id"] == config_id
    assert sum(run["counts"].values()) == 3
    handoffs = database.connection.execute(
        "SELECT count(*) FROM survey_handoffs WHERE run_id=%s", (run["run_id"],)
    ).fetchone()[0]
    assert handoffs == run["counts"].get("accepted", 0)


def test_a_failed_job_records_its_error_and_only_resume_reclaims_it(database):
    active(database)
    spec = {"dataset_id": str(uuid4()), "config_id": str(uuid4())}
    job = database.create_job(str(uuid4()), "batch", spec)
    asyncio.run(run_queue(database, SETTINGS, job_id=job["job_id"], agent_factory=agents))
    record = database.job(job["job_id"])
    assert record["status"] == "failed"
    assert "Unknown configuration version" in record["error"]
    assert database.claim_job(job["job_id"]) is None
    assert database.claim_job(job["job_id"], resume=True)["status"] == "running"


def test_experiment_job_saves_each_candidate_as_a_child_of_the_active_version(database):
    base = active(database)
    spec = ExperimentSpec.model_validate(
        {
            "name": "Queued comparison",
            "insights": [parse_insights(SMOKE.read_bytes())[0]],
            "candidates": [
                {"label": "A", "model": {"model_id": "test-a"}},
                {"label": "B", "model": {"model_id": "test-b"}},
            ],
            "reviewer": {"model_id": "reviewer"},
        }
    )
    key = str(uuid4())
    database.create_experiment(key, spec.model_dump(mode="json"))
    job = database.create_job(str(uuid4()), "experiment", {"experiment_id": key})
    asyncio.run(run_queue(database, SETTINGS, job_id=job["job_id"], agent_factory=agents))
    record = database.job(job["job_id"])
    assert record["status"] == "completed", record["error"]
    configs = {run["config_id"] for run in record["runs"]}
    assert len(configs) == 2
    for config_id in configs:
        version = database.config(config_id)
        assert version["parent_id"] == base
        assert version["generation_model"] in {"test-a", "test-b"}
    assert database.experiment_detail(key)["status"] == "completed"
