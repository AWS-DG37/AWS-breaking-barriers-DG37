"""Filtering and sorting runs and their results against real PostgreSQL, through the
console API. Rollback-only."""

from postgres import requires_postgres
from test_delete_run_postgres import console
from test_improvement_postgres import batch_run
from test_jobs_postgres import active, fund_console

pytestmark = requires_postgres


def ids(response):
    assert response.status_code == 200, response.text
    return [row["insight_id"] for row in response.json()["results"]]


def test_results_filter_by_facets_and_search_and_sort(database):
    fund_console(database, "test")
    run = batch_run(database, active(database))  # The first six smoke insights.
    client = console(database)
    results = f"/api/runs/{run}/results"

    facets = client.get(f"/api/runs/{run}/facets").json()
    assert facets["crops"] == {"wheat": 3, "cow": 1, "mustard": 1, "potato": 1}
    assert facets["concerns"]["pest_disease"] == 2
    assert sum(facets["rules"].values()) == 6

    assert sorted(ids(client.get(results, params={"crop": "wheat"}))) == [
        "I-004101",
        "I-004420",
        "I-004871",
    ]
    assert ids(client.get(results, params={"crop": "wheat", "concern": "water_stress"})) == [
        "I-004871"
    ]
    rule = next(iter(facets["rules"]))
    assert len(ids(client.get(results, params={"rule": rule}))) == facets["rules"][rule]

    # Search covers the farmer's question, case-insensitively, and survey questions.
    assert ids(client.get(results, params={"search": "GREEN INSECTS"})) == ["I-004512"]
    survey = database.run_result(run, "I-004512")["outcome"]["survey"]
    word = survey["items"][-1]["prompt"].split()[-1]
    assert "I-004512" in ids(client.get(results, params={"search": word}))
    # LIKE wildcards are matched literally.
    assert ids(client.get(results, params={"search": "%"})) == []

    def sorted_by(column, direction):
        return client.get(results, params={"sort": column, "direction": direction})

    by_id = ids(sorted_by("insight", "asc"))
    assert by_id == sorted(by_id) and len(by_id) == 6
    assert ids(sorted_by("insight", "desc")) == list(reversed(by_id))
    newest = ids(sorted_by("insight_date", "desc"))
    assert newest[0] == "I-005330" and newest[-1] == "I-004101"
    outcomes = [r["status"] for r in sorted_by("outcome", "asc").json()["results"]]
    rank = ["accepted", "rejected", "skipped", "failed"]
    assert outcomes == sorted(outcomes, key=rank.index)
    for direction in ("asc", "desc"):
        values = [r["relevance"] for r in sorted_by("relevance", direction).json()["results"]]
        scored = [v for v in values if v is not None]
        # Unscored (skipped) results come last in either direction.
        assert values[: len(scored)] == sorted(scored, reverse=direction == "desc")
    paged = client.get(results, params={"sort": "insight", "offset": 2, "limit": 2})
    assert ids(paged) == by_id[2:4]

    assert client.get(results, params={"sort": "random"}).status_code == 422
    assert client.get(results, params={"direction": "up"}).status_code == 422
    assert client.get(results, params={"unknown": "x"}).status_code == 422


def test_runs_filter_by_status_and_version_and_sort(database):
    fund_console(database, "test")
    config_id = active(database)
    first, second = batch_run(database, config_id), batch_run(database, config_id)
    database.finish(first, "failed")
    client = console(database)

    def runs(**params):
        response = client.get("/api/runs", params=params)
        assert response.status_code == 200, response.text
        return [r["run_id"] for r in response.json()["runs"]]

    assert first in runs(status="failed") and second not in runs(status="failed")
    mine = runs(config_id=config_id)
    assert {first, second} <= set(mine)
    other = database.save_config(
        {
            **database.config(config_id)["definition"],
            "prompts": {"generate": "Other.", "review": "Other."},
        }
    )
    assert runs(config_id=other) == []
    # Runs made in one test transaction share a timestamp; the run ID breaks ties.
    assert runs(sort="oldest") == list(reversed(runs(sort="newest")))
    most = client.get("/api/runs", params={"sort": "most_accepted"}).json()["runs"]
    accepted = [r["counts"].get("accepted", 0) for r in most]
    assert accepted == sorted(accepted, reverse=True)
    assert client.get("/api/runs", params={"status": "nope"}).status_code == 422
