"""Deleting runs against real PostgreSQL, through the console API. Rollback-only."""

import asyncio
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from postgres import requires_postgres
from test_improvement_postgres import Shared, batch_run
from test_jobs_postgres import SETTINGS, active, agents, fund_console

from mel.browse import ResultFilter
from mel.costs import cap_view
from mel.experiments import ExperimentSpec, execute_experiment
from mel.explorer.api import create_app

pytestmark = requires_postgres


def console(database):
    return TestClient(
        create_app(
            settings=SETTINGS,
            store_factory=lambda settings: Shared(database),
            dispatcher=lambda settings, job_id: None,
        )
    )


def count(database, table, run_id):
    return database.connection.execute(
        f"SELECT count(*) FROM {table} WHERE run_id=%s", (run_id,)
    ).fetchone()[0]


def test_deleting_a_run_removes_its_records_but_keeps_spend_and_review_comments(database):
    fund_console(database, "test")
    config_id = active(database)
    run_id = batch_run(database, config_id)
    job_id = database.run_summary(run_id)["job_id"]
    accepted = database.run_results(run_id, ResultFilter(status="accepted"))[0]["insight_id"]
    client = console(database)
    list_id = client.post("/api/review-lists", json={"name": "Keep me"}).json()["list_id"]
    item = client.post(
        f"/api/review-lists/{list_id}/items",
        json={"run_id": run_id, "insight_id": accepted, "comment": "Too long"},
    ).json()
    committed = cap_view(database)["committed_usd"]
    assert Decimal(committed) > 0
    assert count(database, "model_calls", run_id) and count(database, "survey_handoffs", run_id)

    deleted = client.delete(f"/api/runs/{run_id}")
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["handoffs"] >= 1 and body["results"] == 6
    assert body["review_comments_kept"] == 1 and body["job_deleted"]
    for table in ("insight_results", "survey_handoffs", "model_calls", "run_costs"):
        assert count(database, table, run_id) == 0, table
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert database.job(job_id) is None
    # The cap still counts what the deleted run spent.
    assert cap_view(database)["committed_usd"] == committed
    (kept,) = client.get(f"/api/review-lists/{list_id}").json()["items"]
    assert kept["item_id"] == item["item_id"] and kept["run_id"] is None
    assert kept["survey"] == item["survey"]
    assert client.delete(f"/api/runs/{run_id}").status_code == 404


def test_comparison_running_and_claimed_runs_are_refused(database):
    fund_console(database, "test")
    config_id = active(database)
    client = console(database)

    experiment = str(uuid4())
    insight = database.run_result(
        run := batch_run(database, config_id),
        database.run_results(run, ResultFilter())[0]["insight_id"],
    )["insight"]
    asyncio.run(
        execute_experiment(
            ExperimentSpec(
                name="Keep",
                insights=[insight],
                candidates=[{"label": "A", "config_id": config_id}],
            ),
            settings=SETTINGS,
            store=database,
            experiment_id=experiment,
            agent_factory=agents,
        )
    )
    (compared,) = database.experiment_detail(experiment)["runs"]
    refused = client.delete(f"/api/runs/{compared['run_id']}")
    assert refused.status_code == 409 and "comparison" in refused.json()["detail"]

    job_id = database.run_summary(run)["job_id"]
    database.connection.execute("UPDATE jobs SET status='running' WHERE job_id=%s", (job_id,))
    running = client.delete(f"/api/runs/{run}")
    assert running.status_code == 409 and "still running" in running.json()["detail"]

    database.connection.execute("UPDATE jobs SET status='completed' WHERE job_id=%s", (job_id,))
    database.connection.execute(
        "UPDATE survey_handoffs SET status='scheduled' WHERE run_id=%s", (run,)
    )
    claimed = client.delete(f"/api/runs/{run}")
    assert claimed.status_code == 409 and "scheduler" in claimed.json()["detail"]
    assert client.get(f"/api/runs/{run}").status_code == 200
