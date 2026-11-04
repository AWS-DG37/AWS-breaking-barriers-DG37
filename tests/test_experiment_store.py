from contextlib import nullcontext
from threading import RLock

import pytest

from mel.models import Outcome
from mel.store import Store


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, purpose="experiment"):
        self.purpose = purpose
        self.statements = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            return Result([(True,)])
        if "SELECT fingerprint" in sql:
            return Result([("fingerprint", self.purpose, "experiment-1")])
        if "SELECT purpose" in sql:
            return Result([(self.purpose,)])
        return Result([])


def store(purpose="experiment"):
    result = Store.__new__(Store)
    result.connection = Connection(purpose)
    result.lock = RLock()
    return result


def test_experiment_accepted_outcome_never_enters_scheduler_outbox():
    db = store()
    db.save(
        "run",
        {"farmer_id": "farmer"},
        Outcome(insight_id="i1", status="accepted", survey={"survey_id": "survey"}),
    )
    assert any("INSERT INTO insight_results" in sql for sql, _ in db.connection.statements)
    assert not any("INSERT INTO survey_handoffs" in sql for sql, _ in db.connection.statements)


def test_resume_cannot_change_purpose_or_experiment_binding():
    db = store()
    with pytest.raises(ValueError, match="purpose|binding"):
        db.start_run("run", "fingerprint", {}, purpose="handoff")
