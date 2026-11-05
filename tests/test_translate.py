"""English versions of saved surveys for the console phone's language switch."""

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from memory_configs import MemoryConfigs
from postgres import database, requires_postgres  # noqa: F401  (pytest fixture)
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from mel.config import ModelConfig
from mel.costs import CapUpdate, RunCosts, cap_view, set_cap
from mel.explorer.api import create_app
from mel.models import Outcome
from mel.translate import SurveyTranslation, english_items, request_text, survey_key

RUN = "00000000-0000-0000-0000-000000000001"
SURVEY = {
    "survey_id": "S-1",
    "language": "hi-IN",
    "items": [
        {
            "item_id": "q1",
            "type": "survey-confirm",
            "prompt": "क्या आपने घोल दिया?",
            "options": [
                {"value": "yes", "label": "हाँ"},
                {"value": "no", "label": "नहीं", "follow_up": "advice_did_not_work"},
            ],
        },
        {
            "item_id": "q2",
            "type": "survey-scale",
            "prompt": "कितना?",
            "scale_min": 0,
            "scale_max": 5,
        },
    ],
}
ENGLISH = {
    "items": [
        {"item_id": "q1", "prompt": "Did you apply the drench?", "labels": ["Yes", "No"]},
        {"item_id": "q2", "prompt": "How much?", "labels": []},
    ]
}


def test_english_items_swap_words_and_keep_ids_values_and_follow_ups():
    items = english_items(SURVEY, SurveyTranslation.model_validate(ENGLISH))
    assert [i["prompt"] for i in items] == ["Did you apply the drench?", "How much?"]
    assert items[0]["options"] == [
        {"value": "yes", "label": "Yes"},
        {"value": "no", "label": "No", "follow_up": "advice_did_not_work"},
    ]
    assert items[1]["scale_max"] == 5 and "options" not in items[1]
    assert SURVEY["items"][0]["prompt"].startswith("क्या")  # the original is untouched


@pytest.mark.parametrize(
    "broken",
    [
        {"items": [ENGLISH["items"][0]]},  # a question is missing
        {"items": [{**ENGLISH["items"][0], "labels": ["Yes"]}, ENGLISH["items"][1]]},
        {"items": [*ENGLISH["items"], {"item_id": "q9", "prompt": "Extra", "labels": []}]},
        {"items": [ENGLISH["items"][0], ENGLISH["items"][0], ENGLISH["items"][1]]},
    ],
)
def test_a_translation_that_does_not_match_the_survey_is_refused(broken):
    with pytest.raises(ValueError):
        english_items(SURVEY, SurveyTranslation.model_validate(broken))


def test_the_key_follows_the_survey_text_and_the_request_lists_every_label():
    assert survey_key(SURVEY) == survey_key({**SURVEY, "survey_id": "other"})
    changed = {**SURVEY, "items": [{**SURVEY["items"][0], "prompt": "नया"}, SURVEY["items"][1]]}
    assert survey_key(changed) != survey_key(SURVEY)
    assert "नहीं" in request_text(SURVEY) and '"labels": []' in request_text(SURVEY)


class Store(MemoryConfigs):
    def __init__(self, survey=SURVEY):
        super().__init__()
        self.result = {"status": "accepted", "outcome": {"survey": survey}}
        self.saved = {}

    def initialize(self):
        pass

    def close(self):
        pass

    def run_result(self, run_id, insight_id):
        return self.result if insight_id == "I-1" else None

    def run_summary(self, run_id):
        return {"config_id": None}

    def translation(self, key):
        return self.saved.get(key)

    def save_translation(self, key, language, items, run_id, model_id):
        self.saved.setdefault(key, {"language": language, "items": items})
        return self.saved[key]


def translator(output, calls):
    def build(model, output_type, prompt, *args):
        calls.append(model.model_id)
        return Agent(TestModel(custom_output_args=output), output_type=output_type)

    return build


def test_the_console_translates_a_saved_survey_once_and_then_serves_it():
    store, calls = Store(), []
    app = create_app(store_factory=lambda _: store, translation_agent=translator(ENGLISH, calls))
    path = f"/api/runs/{RUN}/results/I-1/english"
    with TestClient(app) as client:
        assert client.get(path).json() is None  # none yet: only this lets the page pay
        made = client.post(path)
        assert made.status_code == 200, made.text
        assert made.json()["language"] == "en-IN"
        assert made.json()["items"][0]["options"][1]["label"] == "No"
        assert client.get(path).json() == made.json()
        assert client.post(path).json() == made.json()
    assert len(calls) == 1  # the second request reused the stored version


def test_refusals_never_reach_a_model():
    calls = []
    english = Store({**SURVEY, "language": "en-IN"})
    path = f"/api/runs/{RUN}/results/I-1/english"
    with TestClient(create_app(store_factory=lambda _: english,
                               translation_agent=translator(ENGLISH, calls))) as client:
        assert client.post(path).status_code == 409
        assert client.post(f"/api/runs/{RUN}/results/I-404/english").status_code == 404
    offline = create_app(store_factory=lambda _: Store(), live_enabled=False,
                         translation_agent=translator(ENGLISH, calls))  # fmt: skip
    with TestClient(offline) as client:
        assert client.post(path).status_code == 403
    assert calls == []


def test_a_mismatched_translation_is_an_error_and_is_not_stored():
    store = Store()
    wrong = {"items": [{**ENGLISH["items"][0], "labels": ["Yes"]}, ENGLISH["items"][1]]}
    app = create_app(store_factory=lambda _: store, translation_agent=translator(wrong, []))
    with TestClient(app) as client:
        response = client.post(f"/api/runs/{RUN}/results/I-1/english")
    assert response.status_code == 502
    assert "Translation failed" in response.json()["detail"]
    assert store.saved == {}


@requires_postgres
def test_translation_is_charged_to_the_run_under_the_console_cap(database):  # noqa: F811
    run = str(uuid4())
    database.start_run(run, "translate", {})
    insight = {"insight_id": "I-1", "farmer_id": "F-1"}
    # A rejected draft: the console translates drafts too, and nothing is queued to send.
    database.save(
        run, insight, Outcome(insight_id="I-1", status="rejected", candidate_survey=SURVEY)
    )
    database.finish(run, "completed")
    database.close = lambda: None  # the fixture owns the connection
    calls = []
    app = create_app(store_factory=lambda _: database, translation_agent=translator(ENGLISH, calls))
    path = f"/api/runs/{run}/results/I-1/english"
    with TestClient(app) as client:
        refused = client.post(path)  # no console spend cap is set
        assert refused.status_code == 409 and "spend cap" in refused.json()["detail"]
        version = cap_view(database)["version"]
        set_cap(database, CapUpdate(expected_version=version, budget_usd=Decimal("5")))
        made = client.post(path)
        assert made.status_code == 200, made.text
        assert client.get(path).json() == made.json()
    rows = database.connection.execute(
        "SELECT role,state FROM model_calls WHERE run_id=%s ORDER BY created_at", (run,)
    ).fetchall()
    assert rows == [("translate", "complete")]  # the refusal made no call
    stop = database.connection.execute(
        "SELECT stop_reason FROM run_costs WHERE run_id=%s", (run,)
    ).fetchone()[0]
    assert stop is None  # a refused translation never stops the run
    assert database.translation(survey_key(SURVEY))["items"][0]["prompt"] == ENGLISH["items"][0][
        "prompt"
    ]


@requires_postgres
def test_a_failed_translation_never_blocks_a_budgeted_run(database):  # noqa: F811
    run = str(uuid4())
    database.start_run(run, "translate-budget", {})
    insight = {"insight_id": "I-1", "farmer_id": "F-1"}
    database.save(
        run, insight, Outcome(insight_id="I-1", status="rejected", candidate_survey=SURVEY)
    )
    # A console run: inside the cap, with its own budget, as every console launch has.
    RunCosts(database, run).initialize(Decimal("5"), capped=True)
    version = cap_view(database)["version"]
    set_cap(database, CapUpdate(expected_version=version, budget_usd=Decimal("5")))
    database.close = lambda: None

    def outage(messages, info):
        raise RuntimeError("provider unavailable")

    def failing(model, output_type, prompt, *args):
        return Agent(FunctionModel(outage), output_type=output_type)

    path = f"/api/runs/{run}/results/I-1/english"
    with TestClient(create_app(store_factory=lambda _: database, translation_agent=failing)) as c:
        assert c.post(path).status_code == 502
    with TestClient(
        create_app(store_factory=lambda _: database, translation_agent=translator(ENGLISH, []))
    ) as client:
        assert client.post(path).status_code == 200  # the unsettled call does not block it
    states = database.connection.execute(
        "SELECT role,state FROM model_calls WHERE run_id=%s ORDER BY created_at", (run,)
    ).fetchall()
    assert states == [("translate", "unknown"), ("translate", "complete")]
    stop = database.connection.execute(
        "SELECT stop_reason FROM run_costs WHERE run_id=%s", (run,)
    ).fetchone()[0]
    assert stop is None
    # The run's own next request is still allowed: translations never touch its budget.
    luna = ModelConfig(model_id="openai.gpt-6-luna", api="responses", max_tokens=1800)
    assert RunCosts(database, run).reserve(luna, "us-east-1", "generate", 1, "I-2")


@requires_postgres
def test_a_command_line_run_cannot_take_translations_outside_the_cap(database):  # noqa: F811
    run = str(uuid4())
    database.start_run(run, "translate-cli", {})
    insight = {"insight_id": "I-1", "farmer_id": "F-1"}
    database.save(
        run, insight, Outcome(insight_id="I-1", status="rejected", candidate_survey=SURVEY)
    )
    database.finish(run, "completed")
    # A command-line run: its cost record exists and sits outside the console cap.
    database.connection.execute(
        "INSERT INTO run_costs(run_id,budget_usd,capped) VALUES (%s,NULL,false)", (run,)
    )
    database.close = lambda: None
    app = create_app(store_factory=lambda _: database, translation_agent=translator(ENGLISH, []))
    path = f"/api/runs/{run}/results/I-1/english"
    with TestClient(app) as client:
        refused = client.post(path)
        assert refused.status_code == 409 and "spend cap" in refused.json()["detail"]
        version = cap_view(database)["version"]
        set_cap(database, CapUpdate(expected_version=version, budget_usd=Decimal("5")))
        before = Decimal(cap_view(database)["committed_usd"])
        assert client.post(path).status_code == 200
    after = Decimal(cap_view(database)["committed_usd"])
    assert after > before  # the translation counts toward the cap
    database.delete_run(run)
    assert Decimal(cap_view(database)["committed_usd"]) == after  # and keeps counting
