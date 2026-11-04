"""Message history log against real PostgreSQL.

Rollback-only, following tests/postgres.py and test_jobs_postgres.py. Set
MEL_TEST_DB_PORT to run; skipped otherwise.

These verify that message events reported by an external sender are recorded with
their own timestamps - sent/delivered/failed kept separate from our created_at -
normalized to IST, and that the actual sent_at is taken from the reported event, never
inferred from the survey's calculated send window.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from postgres import requires_postgres

from mel.clock import IST

pytestmark = requires_postgres

PACK = Path("docs/participant-pack")


def _make_handoff_survey(db) -> str:
    """Create a real survey_handoffs row so a message can reference a survey_id via the
    foreign key, reusing the store's own save path. Returns the survey_id."""
    from mel.models import Outcome

    scenario = PACK / "json/scenarios/1-mustard-aphid-spray"
    insight = json.loads((scenario / "insight.json").read_text())
    expected = json.loads((scenario / "schedule_handoff.json").read_text())
    survey = expected["survey"]
    run_id = str(uuid4())
    db.start_run(run_id, "fingerprint", {"selection": {}})
    outcome = Outcome(
        insight_id=insight["insight_id"],
        status="accepted",
        survey=survey,
        handoff=expected,
    )
    db.save(run_id, insight, outcome)
    return survey["survey_id"]


def test_logs_a_sent_message_and_reads_it_back(database):
    record = database.log_message(
        farmer_id="F-000123",
        channel="whatsapp",
        message_type="initial_survey",
        status="sent",
        sent_at="2026-12-06T09:05:00+05:30",
        external_message_id="wamid.ABC",
    )
    assert record["farmer_id"] == "F-000123"
    assert record["channel"] == "whatsapp"
    assert record["status"] == "sent"
    assert record["sent_at"] == "2026-12-06T09:05:00+05:30"
    # created_at is our own insert time, set by the database and distinct from sent_at.
    assert record["created_at"] is not None
    assert record["created_at"] != record["sent_at"]
    assert database.message(record["id"]) == record


def test_utc_event_time_is_stored_as_ist(database):
    # 2026-12-06T03:35:00Z == 2026-12-06T09:05:00+05:30
    record = database.log_message(
        farmer_id="F-000123",
        channel="app",
        message_type="initial_survey",
        status="delivered",
        sent_at=datetime(2026, 12, 6, 3, 35, tzinfo=UTC),
        delivered_at="2026-12-06T03:40:00Z",
    )
    assert record["sent_at"] == "2026-12-06T09:05:00+05:30"
    assert record["delivered_at"] == "2026-12-06T09:10:00+05:30"
    sent = datetime.fromisoformat(record["sent_at"])
    assert sent.utcoffset() == IST.utcoffset(sent)


def test_naive_event_time_is_rejected(database):
    with pytest.raises(ValueError):
        database.log_message(
            farmer_id="F-000123",
            channel="whatsapp",
            message_type="initial_survey",
            status="sent",
            sent_at="2026-12-06T09:05:00",  # no offset
        )


def test_failed_message_may_omit_sent_at(database):
    record = database.log_message(
        farmer_id="F-000123",
        channel="voice",
        message_type="reminder",
        status="failed",
        failed_at="2026-12-06T10:00:00+05:30",
    )
    assert record["status"] == "failed"
    assert record["sent_at"] is None
    assert record["failed_at"] == "2026-12-06T10:00:00+05:30"
    assert record["delivered_at"] is None


def test_optional_fields_default_to_none(database):
    record = database.log_message(
        farmer_id="F-000999",
        channel="telegram",
        message_type="other",
        status="sent",
        sent_at="2026-12-06T09:05:00+05:30",
    )
    assert record["survey_id"] is None
    assert record["message_content"] is None
    assert record["external_message_id"] is None
    assert record["delivered_at"] is None
    assert record["failed_at"] is None


def test_message_content_roundtrips(database):
    content = {"text": "A quick question about your advice", "items": 3}
    record = database.log_message(
        farmer_id="F-000123",
        channel="whatsapp",
        message_type="initial_survey",
        status="sent",
        sent_at="2026-12-06T09:05:00+05:30",
        message_content=content,
    )
    assert record["message_content"] == content


def test_retrieve_by_farmer_in_insertion_order(database):
    database.log_message(
        farmer_id="F-aaa", channel="app", message_type="initial_survey", status="sent",
        sent_at="2026-12-06T09:05:00+05:30",
    )
    database.log_message(
        farmer_id="F-aaa", channel="whatsapp", message_type="reminder", status="sent",
        sent_at="2026-12-07T09:05:00+05:30",
    )
    database.log_message(
        farmer_id="F-bbb", channel="voice", message_type="initial_survey", status="failed",
        failed_at="2026-12-06T09:05:00+05:30",
    )
    aaa = database.messages_for_farmer("F-aaa")
    assert [m["channel"] for m in aaa] == ["app", "whatsapp"]
    assert {m["farmer_id"] for m in aaa} == {"F-aaa"}
    assert len(database.messages_for_farmer("F-bbb")) == 1
    assert database.messages_for_farmer("F-none") == []


def test_retrieve_by_survey(database):
    survey_id = _make_handoff_survey(database)
    database.log_message(
        farmer_id="F-000123", survey_id=survey_id, channel="app",
        message_type="initial_survey", status="sent", sent_at="2026-12-06T09:05:00+05:30",
    )
    database.log_message(
        farmer_id="F-000123", survey_id=survey_id, channel="whatsapp",
        message_type="reminder", status="sent", sent_at="2026-12-08T09:05:00+05:30",
    )
    logs = database.messages_for_survey(survey_id)
    assert len(logs) == 2
    assert all(m["survey_id"] == survey_id for m in logs)
    assert [m["message_type"] for m in logs] == ["initial_survey", "reminder"]


def test_duplicate_external_message_id_is_not_logged_twice(database):
    first = database.log_message(
        farmer_id="F-000123", channel="whatsapp", message_type="initial_survey",
        status="sent", sent_at="2026-12-06T09:05:00+05:30", external_message_id="wamid.DUP",
    )
    # A re-report of the same platform id returns the existing row, not a new one.
    second = database.log_message(
        farmer_id="F-000123", channel="whatsapp", message_type="initial_survey",
        status="delivered", sent_at="2026-12-06T09:06:00+05:30",
        delivered_at="2026-12-06T09:07:00+05:30", external_message_id="wamid.DUP",
    )
    assert second["id"] == first["id"]
    assert second["status"] == "sent"  # unchanged; the first record is preserved
    assert len(database.messages_for_farmer("F-000123")) == 1


def test_sent_at_is_not_inferred_from_the_survey_send_window(database):
    # The survey's calculated window must never masquerade as the actual send time.
    # With no reported sent_at, a failed event is logged with sent_at NULL - the store
    # does not reach into the handoff's send_window / not_before to fill it in.
    survey_id = _make_handoff_survey(database)
    window = database.connection.execute(
        "SELECT not_before, not_after FROM survey_handoffs WHERE survey_id=%s", (survey_id,)
    ).fetchone()
    record = database.log_message(
        farmer_id="F-000123", survey_id=survey_id, channel="voice",
        message_type="initial_survey", status="failed",
        failed_at="2026-12-06T10:00:00+05:30",
    )
    assert record["sent_at"] is None
    # And when a real sent_at is supplied, it is exactly what the sender reported,
    # independent of the window bounds.
    sent = database.log_message(
        farmer_id="F-000123", survey_id=survey_id, channel="app",
        message_type="initial_survey", status="sent",
        sent_at="2026-12-07T11:22:00+05:30",
    )
    assert sent["sent_at"] == "2026-12-07T11:22:00+05:30"
    assert sent["sent_at"] not in (window[0].isoformat(), window[1].isoformat())


# ---------------------------------------------------------------------------
# Improvement 1: status <-> timestamp consistency (migration 007)
# ---------------------------------------------------------------------------


def test_sent_requires_sent_at(database):
    with pytest.raises(Exception):  # psycopg raises on the CHECK violation
        database.log_message(
            farmer_id="F-1", channel="app", message_type="initial_survey", status="sent",
        )


def test_delivered_requires_both_sent_at_and_delivered_at(database):
    # delivered without delivered_at violates the new constraint...
    with pytest.raises(Exception):
        database.log_message(
            farmer_id="F-1", channel="app", message_type="initial_survey", status="delivered",
            sent_at="2026-12-06T09:05:00+05:30",
        )
    # ...and with both present it is accepted.
    record = database.log_message(
        farmer_id="F-1", channel="app", message_type="initial_survey", status="delivered",
        sent_at="2026-12-06T09:05:00+05:30", delivered_at="2026-12-06T09:07:00+05:30",
    )
    assert record["status"] == "delivered"
    assert record["sent_at"] == "2026-12-06T09:05:00+05:30"
    assert record["delivered_at"] == "2026-12-06T09:07:00+05:30"


def test_failed_requires_failed_at_but_sent_at_stays_optional(database):
    # failed without failed_at is rejected...
    with pytest.raises(Exception):
        database.log_message(
            farmer_id="F-1", channel="voice", message_type="reminder", status="failed",
        )
    # ...failed with failed_at and no sent_at is allowed...
    no_send = database.log_message(
        farmer_id="F-1", channel="voice", message_type="reminder", status="failed",
        failed_at="2026-12-06T10:00:00+05:30",
    )
    assert no_send["sent_at"] is None and no_send["failed_at"] == "2026-12-06T10:00:00+05:30"
    # ...and a failure that did have a send time keeps it.
    after_send = database.log_message(
        farmer_id="F-1", channel="voice", message_type="reminder", status="failed",
        sent_at="2026-12-06T09:58:00+05:30", failed_at="2026-12-06T10:00:00+05:30",
    )
    assert after_send["sent_at"] == "2026-12-06T09:58:00+05:30"


# ---------------------------------------------------------------------------
# Improvement 2: concurrent duplicate external_message_id across Store instances
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_external_id_creates_one_row_and_never_raises():
    import os
    from concurrent.futures import ThreadPoolExecutor

    from mel.config import Settings
    from mel.store import Store

    settings = Settings(
        database_host="localhost",
        database_port=int(os.environ["MEL_TEST_DB_PORT"]),
        database_sslmode="disable",
    )
    first, second = Store(settings), Store(settings)
    schema = "test_msglog_" + uuid4().hex
    try:
        first.connection.execute(f'CREATE SCHEMA "{schema}"')
        for store in (first, second):
            store.connection.execute(f'SET search_path TO "{schema}"')
        first.initialize()

        def attempt(store):
            # Each call reports the SAME platform id from a separate connection.
            return store.log_message(
                farmer_id="F-race", channel="whatsapp", message_type="initial_survey",
                status="sent", sent_at="2026-12-06T09:05:00+05:30",
                external_message_id="wamid.RACE",
            )

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, (first, second)))

        # Neither caller raised; both got a record with the same id.
        assert all(r is not None for r in results)
        assert results[0]["id"] == results[1]["id"]
        # Exactly one row exists for that external id.
        count = first.connection.execute(
            "SELECT count(*) FROM message_logs WHERE external_message_id=%s", ("wamid.RACE",)
        ).fetchone()[0]
        assert count == 1
    finally:
        second.close()
        first.connection.execute("SET search_path TO public")
        first.connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        first.close()


def test_duplicate_call_with_different_metadata_returns_existing_unchanged(database):
    first = database.log_message(
        farmer_id="F-1", channel="whatsapp", message_type="initial_survey", status="sent",
        sent_at="2026-12-06T09:05:00+05:30", external_message_id="wamid.X",
        message_content={"a": 1},
    )
    second = database.log_message(
        farmer_id="F-1", channel="app", message_type="reminder", status="delivered",
        sent_at="2026-12-06T09:06:00+05:30", delivered_at="2026-12-06T09:07:00+05:30",
        external_message_id="wamid.X", message_content={"a": 2},
    )
    assert second == first  # existing row returned unchanged, different metadata ignored


# ---------------------------------------------------------------------------
# Improvement 3: history ordered by event time, not insertion order
# ---------------------------------------------------------------------------


def test_farmer_history_is_ordered_by_event_time_not_insertion(database):
    # Insert out of event-time order: the second row sent EARLIER than the first.
    database.log_message(
        farmer_id="F-ord", channel="app", message_type="reminder", status="sent",
        sent_at="2026-12-08T09:00:00+05:30", external_message_id="e2",
    )
    database.log_message(
        farmer_id="F-ord", channel="whatsapp", message_type="initial_survey", status="sent",
        sent_at="2026-12-06T09:00:00+05:30", external_message_id="e1",
    )
    history = database.messages_for_farmer("F-ord")
    assert [m["external_message_id"] for m in history] == ["e1", "e2"]
    assert [m["sent_at"] for m in history] == [
        "2026-12-06T09:00:00+05:30",
        "2026-12-08T09:00:00+05:30",
    ]


def test_delivered_event_is_ordered_by_delivered_at_not_sent_at(database):
    # A sent event and a delivered event. The delivered event's sent_at is EARLIER than
    # the other's sent_at, but its delivered_at is LATER - it must sort by delivered_at.
    database.log_message(
        farmer_id="F-d", channel="app", message_type="initial_survey", status="delivered",
        sent_at="2026-12-06T08:00:00+05:30", delivered_at="2026-12-06T12:00:00+05:30",
        external_message_id="d-delivered",
    )
    database.log_message(
        farmer_id="F-d", channel="whatsapp", message_type="reminder", status="sent",
        sent_at="2026-12-06T10:00:00+05:30", external_message_id="d-sent",
    )
    history = database.messages_for_farmer("F-d")
    # Ordered by event time: sent@10:00 before delivered@12:00 (NOT by delivered's
    # sent_at of 08:00, which would wrongly put it first).
    assert [m["external_message_id"] for m in history] == ["d-sent", "d-delivered"]


def test_survey_history_is_ordered_by_event_time(database):
    survey_id = _make_handoff_survey(database)
    database.log_message(
        farmer_id="F-000123", survey_id=survey_id, channel="app",
        message_type="reminder", status="failed", failed_at="2026-12-09T09:00:00+05:30",
        external_message_id="s-late",
    )
    database.log_message(
        farmer_id="F-000123", survey_id=survey_id, channel="whatsapp",
        message_type="initial_survey", status="sent", sent_at="2026-12-06T09:00:00+05:30",
        external_message_id="s-early",
    )
    history = database.messages_for_survey(survey_id)
    assert [m["external_message_id"] for m in history] == ["s-early", "s-late"]
