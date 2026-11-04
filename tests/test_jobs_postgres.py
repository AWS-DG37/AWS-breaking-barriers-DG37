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
from test_costs import price
from test_experiments import versions
from test_trace import dependencies

from mel.bundle import Bundle
from mel.config import Settings
from mel.costs import CapUpdate, NewPrice, cap_view, save_price, set_cap
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


def fund_console(db, *model_ids):
    """Console jobs need verified prices and a spend cap before any model request."""
    for model_id in model_ids:
        for api in ("responses", "converse"):
            pricing = price(max_input_tokens=1_000_000)
            save_price(
                db, NewPrice(model_id=model_id, api=api, region="us-east-1", pricing=pricing)
            )
    version = cap_view(db)["version"]
    set_cap(db, CapUpdate(expected_version=version, budget_usd="1000"))


def active(db):
    key = db.save_config(Bundle.load(SETTINGS).definition, note="test")
    db.name_config(key, "Test baseline")
    db.activate_config(key)
    return key


def test_identical_definitions_share_one_version_and_one_version_is_active(database):
    definition = Bundle.load(SETTINGS).definition
    first = database.save_config(definition, note="first")
    assert database.save_config(definition, note="again") == first
    changed = {**definition, "prompts": {**definition["prompts"], "generate": "Changed."}}
    second = database.save_config(changed, parent_id=first, note="prompt edit", name="Test")
    assert (database.config(second)["status"], database.config(second)["name"]) == ("draft", "Test")
    with pytest.raises(ValueError, match="save this version"):
        database.activate_config(second)
    database.name_config(first, "Baseline")
    database.name_config(second, "Prompt edit", "Shorter generate prompt")
    assert database.config(second)["status"] == "saved"
    assert database.config(second)["note"] == "Shorter generate prompt"
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
    fund_console(database, "test")
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


def test_experiment_job_runs_each_candidate_with_its_own_version(database):
    fund_console(database, "test", "test-a", "test-b")
    first, second = versions(database)
    spec = ExperimentSpec.model_validate(
        {
            "name": "Queued comparison",
            "insights": [parse_insights(SMOKE.read_bytes())[0]],
            "candidates": [{"label": "A", "config_id": first}, {"label": "B", "config_id": second}],
        }
    )
    key = str(uuid4())
    database.create_experiment(key, spec.model_dump(mode="json"))
    job = database.create_job(str(uuid4()), "experiment", {"experiment_id": key})
    asyncio.run(run_queue(database, SETTINGS, job_id=job["job_id"], agent_factory=agents))
    record = database.job(job["job_id"])
    assert record["status"] == "completed", record["error"]
    assert record["progress"] == {"done": 2, "total": 2}
    assert {run["config_id"] for run in record["runs"]} == {first, second}
    detail = database.experiment_detail(key)
    assert detail["status"] == "completed"
    assert detail["job"]["status"] == "completed" and detail["job"]["progress"]["done"] == 2
    assert {run["model_id"] for run in detail["runs"]} == {"test-a", "test-b"}


def test_facets_counts_and_the_runs_view_describe_a_batch(database):
    fund_console(database, "test")
    config_id = active(database)
    dataset = smoke_dataset(database)
    key = dataset["dataset_id"]
    facets = database.dataset_facets(key)
    assert {"value": "wheat", "count": 5} in facets["crop"]
    assert facets["created"] == {"from": "2026-11-15", "to": "2027-03-20"}
    assert database.count_insights(key, InsightFilter(crop="wheat", limit=2)) == 5
    spec = BatchJobSpec(dataset_id=key, config_id=config_id, selection=InsightFilter(limit=4))
    job = database.create_job(str(uuid4()), "batch", spec.model_dump(mode="json"))
    asyncio.run(run_queue(database, SETTINGS, job_id=job["job_id"], agent_factory=agents))
    (run,) = database.job(job["job_id"])["runs"]
    summary = database.run_summary(run["run_id"])
    assert summary["job_id"] == job["job_id"] and summary["progress"] == {"done": 4, "total": 4}
    assert sum(summary["counts"].values()) == 4
    assert run["run_id"] in {r["run_id"] for r in database.list_runs()}
    rows = database.run_results(run["run_id"])
    assert len(rows) == 4
    accepted = [r for r in rows if r["status"] == "accepted"]
    assert accepted and all(r["first_question"] and r["questions"] >= 1 for r in accepted)
    skipped = database.run_results(run["run_id"], status="skipped")
    assert all(r["rule"] and r["rule"] != "R8" for r in skipped)
    assert database.run_results(run["run_id"], offset=3, limit=5)[0] == rows[3]
    one = database.run_result(run["run_id"], rows[0]["insight_id"])
    assert one["outcome"]["insight_id"] == rows[0]["insight_id"]
    assert database.run_result(run["run_id"], "missing") is None
