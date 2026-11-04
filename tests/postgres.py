"""Rollback-only PostgreSQL fixture for integration tests. Set MEL_TEST_DB_PORT to run."""

import os

import pytest

from mel.config import Settings
from mel.store import Store

requires_postgres = pytest.mark.skipif(
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
