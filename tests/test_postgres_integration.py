import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import UniqueViolation

from mel.config import Settings
from mel.models import Outcome, TraceEvent
from mel.store import Store

pytestmark = pytest.mark.skipif(
    not os.environ.get("MEL_TEST_DB_PORT"),
    reason="Set MEL_TEST_DB_PORT for local PostgreSQL checks",
)


@pytest.fixture
def database():
    db = Store(
        Settings(
            database_host="localhost",
            database_port=int(os.environ["MEL_TEST_DB_PORT"]),
            database_sslmode="disable",
        )
    )
    db.initialize()
    db.initialize()
    try:
        with db.connection.transaction(force_rollback=True):
            yield db
    finally:
        db.close()


def test_comparison_results_persist_without_scheduler_handoffs(database):
    experiment, run = str(uuid4()), str(uuid4())
    insight = {"insight_id": "integration-insight", "farmer_id": "integration-farmer"}
    spec = {"name": "Rollback-only integration", "insights": [insight], "candidates": [{}]}
    database.create_experiment(experiment, spec, source="recorded")
    database.start_run(run, "fixed-input", {}, purpose="experiment", experiment_id=experiment)
    event = TraceEvent(sequence=1, stage="decision", source="rule", attempt=0, data={"ok": True})
    database.save(
        run,
        insight,
        Outcome(
            insight_id=insight["insight_id"],
            status="accepted",
            events=[event],
            survey={"survey_id": "candidate-only"},
        ),
    )
    database.finish(run, "completed")
    detail = database.experiment_detail(experiment)
    assert detail["runs"][0]["results"][0]["outcome"]["events"][0]["data"] == {"ok": True}
    assert (
        database.connection.execute(
            "SELECT count(*) FROM survey_handoffs WHERE run_id=%s",
            (run,),
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(ValueError, match="purpose|binding"):
        database.start_run(run, "fixed-input", {}, purpose="handoff")


def test_result_and_handoff_write_roll_back_together(database):
    run = str(uuid4())
    database.start_run(run, "fixed-input", {})
    path = Path("docs/participant-pack/json/scenarios/1-mustard-aphid-spray/survey.hi-IN.json")
    survey = json.loads(path.read_text())
    survey["survey_id"] = "S-" + str(uuid4().int)[:10]
    first = {"insight_id": "first", "farmer_id": survey["farmer_id"]}
    second = {"insight_id": "second", "farmer_id": survey["farmer_id"]}
    database.save(
        run,
        first,
        Outcome(
            insight_id="first",
            status="accepted",
            survey=survey,
            handoff={"survey": survey},
        ),
    )
    with pytest.raises(UniqueViolation):
        database.save(
            run,
            second,
            Outcome(
                insight_id="second",
                status="accepted",
                survey=survey,
                handoff={"survey": survey},
            ),
        )
    assert [r["insight_id"] for r in database.results(run)] == ["first"]
    assert (
        database.connection.execute(
            "SELECT count(*) FROM survey_handoffs WHERE run_id=%s",
            (run,),
        ).fetchone()[0]
        == 1
    )


def test_migrations_run_once_and_changed_history_is_rejected(database, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "migrations"
    folder.mkdir()
    key = uuid4().hex
    migration = folder / f"999_{key}.sql"
    table = f"migration_probe_{key}"
    migration.write_text(
        f"CREATE TABLE IF NOT EXISTS {table}(value int); INSERT INTO {table} VALUES(1);"
    )
    database.initialize()
    database.initialize()
    assert database.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    migration.write_text(migration.read_text() + "\nSELECT 1;\n")
    with pytest.raises(ValueError, match="changed|checksum"):
        database.initialize()
