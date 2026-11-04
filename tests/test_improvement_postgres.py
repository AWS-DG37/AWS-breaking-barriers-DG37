"""Review lists and the improvement loop against real PostgreSQL, through the console API
and the job runner. Rollback-only; models are scripted."""

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient
from improvement_fakes import edit, improver_factory
from postgres import requires_postgres
from test_jobs_postgres import SETTINGS, active, agents, fund_console, smoke_dataset

from mel.browse import ResultFilter
from mel.datasets import InsightFilter
from mel.explorer.api import create_app
from mel.jobs import BatchJobSpec, run_queue

pytestmark = requires_postgres


class Shared:
    """The test's rollback-only connection, which the API must not close."""

    def __init__(self, database):
        self.database = database

    def __getattr__(self, name):
        return getattr(self.database, name)

    def initialize(self):
        pass

    def close(self):
        pass


def batch_run(database, config_id) -> str:
    dataset = smoke_dataset(database)
    spec = BatchJobSpec(
        dataset_id=dataset["dataset_id"], config_id=config_id, selection=InsightFilter(limit=6)
    )
    job = database.create_job(str(uuid4()), "batch", spec.model_dump(mode="json"))
    asyncio.run(run_queue(database, SETTINGS, job_id=job["job_id"], agent_factory=agents))
    (run,) = database.job(job["job_id"])["runs"]
    return run["run_id"]


def test_review_list_drives_an_improvement_job_that_saves_a_draft_and_meters_its_calls(
    database,
):
    fund_console(database, "test", "improver")
    config_id = active(database)
    run_id = batch_run(database, config_id)
    accepted = database.run_results(run_id, ResultFilter(status="accepted"))[0]["insight_id"]
    skipped = database.run_results(run_id, ResultFilter(status="skipped"))[0]["insight_id"]
    client = TestClient(
        create_app(
            settings=SETTINGS,
            store_factory=lambda settings: Shared(database),
            dispatcher=lambda settings, job_id: None,
        )
    )

    created = client.post("/api/review-lists", json={"name": "Vague outcomes"})
    assert created.status_code == 201, created.text
    list_id = created.json()["list_id"]
    survey = database.run_result(run_id, accepted)["outcome"]["survey"]
    question = survey["items"][0]["item_id"]
    target = {"run_id": run_id, "insight_id": accepted}
    first = client.post(
        f"/api/review-lists/{list_id}/items",
        json={**target, "question_id": question, "comment": "Too vague", "should_be": "Did X?"},
    )
    assert first.status_code == 201, first.text
    assert first.json()["survey"] == survey and first.json()["config_id"] == config_id
    again = client.post(
        f"/api/review-lists/{list_id}/items",
        json={**target, "question_id": question, "comment": "Name the product"},
    )
    assert again.json()["item_id"] == first.json()["item_id"]  # Same question: replaced.
    whole = client.post(
        f"/api/review-lists/{list_id}/items", json={**target, "comment": "Ask the outcome first"}
    )
    assert whole.status_code == 201
    bad = client.post(
        f"/api/review-lists/{list_id}/items",
        json={**target, "question_id": "nope", "comment": "x"},
    )
    assert bad.status_code == 422
    no_survey = client.post(
        f"/api/review-lists/{list_id}/items",
        json={"run_id": run_id, "insight_id": skipped, "comment": "x"},
    )
    assert no_survey.status_code == 422
    edited = client.put(
        f"/api/review-items/{whole.json()['item_id']}",
        json={"comment": "Ask the outcome first", "should_be": "How is the crop now?"},
    )
    assert edited.json()["should_be"] == "How is the crop now?"
    listed = client.get(f"/api/review-lists/{list_id}").json()
    assert (listed["item_count"], listed["insight_count"]) == (2, 1)
    # One test transaction gives every row the same now(); compare without order.
    assert {i["comment"] for i in listed["items"]} == {"Name the product", "Ask the outcome first"}

    started = client.post(
        f"/api/review-lists/{list_id}/improvements",
        json={
            "base_config_id": config_id,
            "compare_config_id": config_id,
            "improver": {"model_id": "improver", "max_tokens": 4000},
            "rounds": 1,
            "budget_usd": "50",
        },
    )
    assert started.status_code == 202, started.text
    key, job_id = started.json()["improvement_id"], started.json()["job_id"]
    # Comments are frozen into the loop: deleting one now does not change it.
    assert client.delete(f"/api/review-items/{first.json()['item_id']}").status_code == 204

    first_line = database.config(config_id)["definition"]["prompts"]["generate"].splitlines()[0]
    factory = improver_factory([edit(first_line, "Better opening.")], [0.3, 0.8])
    asyncio.run(
        run_queue(database, SETTINGS, job_id=job_id, agent_factory=agents, improver_factory=factory)
    )
    record = client.get(f"/api/improvements/{key}").json()
    assert record["status"] == "completed", record["error"]
    assert record["job"]["status"] == "completed"
    assert record["job"]["progress"] == {"done": 2, "total": 2, "unit": "evaluations"}
    assert len(record["spec"]["items"]) == 2
    assert record["verdict"]["improved"] and record["verdict"]["score_delta"] == 0.5
    best = database.config(record["best_config_id"])
    assert (best["status"], best["parent_id"]) == ("draft", config_id)
    assert best["note"] == "Improvement round 1: Ask about the outcome first"
    assert best["definition"]["prompts"]["generate"].startswith("Better opening.")

    base_run = record["rounds"][0]["evaluations"][0]["run_id"]
    round_run = record["rounds"][1]["evaluations"][0]["run_id"]
    roles = dict(
        database.connection.execute(
            "SELECT run_id::text||'/'||role,count(*) FROM model_calls "
            "WHERE run_id = ANY(%s::uuid[]) AND role IN ('improve','judge') GROUP BY 1",
            ([base_run, round_run],),
        ).fetchall()
    )
    # The improver is charged to the run it analysed; each judge to the run it judged.
    assert roles == {f"{base_run}/improve": 1, f"{base_run}/judge": 1, f"{round_run}/judge": 1}
    experiment = client.get(
        f"/api/experiments/{record['rounds'][1]['evaluations'][0]['experiment_id']}"
    )
    assert experiment.status_code == 200


def test_improvements_refuse_unknown_versions_and_empty_lists(database):
    config_id = active(database)
    client = TestClient(
        create_app(
            settings=SETTINGS,
            store_factory=lambda settings: Shared(database),
            dispatcher=lambda settings, job_id: None,
        )
    )
    list_id = client.post("/api/review-lists", json={"name": "Empty"}).json()["list_id"]
    body = {
        "base_config_id": config_id,
        "compare_config_id": config_id,
        "improver": {"model_id": "improver"},
        "rounds": 1,
    }
    empty = client.post(f"/api/review-lists/{list_id}/improvements", json=body)
    assert empty.status_code == 422 and "at least 1" in empty.json()["detail"]
    unknown = client.post(
        f"/api/review-lists/{list_id}/improvements",
        json={**body, "compare_config_id": str(uuid4())},
    )
    assert unknown.status_code == 422
    assert client.get(f"/api/review-lists/{uuid4()}").status_code == 404
