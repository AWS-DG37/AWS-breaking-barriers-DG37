from uuid import uuid4

from fastapi.testclient import TestClient
from memory_configs import MemoryConfigs
from test_experiments import specification

from mel.config import Settings
from mel.explorer.api import create_app


class Store(MemoryConfigs):
    def __init__(self):
        super().__init__()
        self.jobs = []
        self.assessments = []
        self.created = []
        self.closed = 0

    def initialize(self):
        pass

    def close(self):
        self.closed += 1

    def create_experiment(self, key, spec, source="live"):
        self.created.append(key)

    def create_job(self, key, kind, spec):
        self.jobs.append((kind, spec))
        return {"job_id": key, "kind": kind, "spec": spec, "status": "queued"}

    def finish_job(self, key, status, error=None):
        self.finished = (key, status, error)

    def list_experiments(self):
        return []

    def experiment_detail(self, key):
        return {
            "experiment_id": key,
            "spec": {"insights": []},
            "runs": [
                {
                    "run_id": "run-a",
                    "label": "A",
                    "results": [
                        {
                            "insight_id": "i1",
                            "input": {"insight_id": "i1"},
                            "outcome": {
                                "status": "accepted",
                                "survey": {"items": [{"item_id": "q1"}]},
                            },
                        }
                    ],
                },
                {
                    "run_id": "run-b",
                    "label": "B",
                    "results": [
                        {
                            "insight_id": "i1",
                            "input": {"insight_id": "i1"},
                            "outcome": {
                                "status": "accepted",
                                "survey": {"items": [{"item_id": "q1"}]},
                            },
                        }
                    ],
                },
            ],
            "assessments": self.assessments,
        }

    def save_assessment(self, key, assessment):
        self.assessments.append(assessment)
        return assessment


def test_assessments_keep_identical_question_ids_separate_by_run():
    store = Store()
    with TestClient(create_app(store_factory=lambda _: store)) as client:
        rating = {
            "scope": "question",
            "assessor": "human",
            "run_id": "run-a",
            "insight_id": "i1",
            "item_id": "q1",
            "dimension": "clarity",
            "rubric_version": "v1",
            "assessment": "assessed",
            "score": 4,
        }
        response = client.post(f"/api/experiments/{uuid4()}/assessments", json=rating)
        assert response.status_code == 200
        assert store.assessments[0]["run_id"] == "run-a"
        assert not [a for a in store.assessments if a["run_id"] == "run-b"]
        rating["run_id"] = "alien-run"
        assert (
            client.post(f"/api/experiments/{uuid4()}/assessments", json=rating).status_code == 422
        )
        rating["assessor"] = "model"
        assert (
            client.post(f"/api/experiments/{uuid4()}/assessments", json=rating).status_code == 422
        )


def test_unavailable_database_and_catalog_are_visible_without_secrets():
    def broken(_):
        raise RuntimeError("secret-token")

    async def catalog(_):
        raise RuntimeError("secret-token")

    with TestClient(create_app(store_factory=broken, catalog_loader=catalog)) as client:
        health = client.get("/api/health")
        assert health.json()["database"] == "unavailable"
        assert "secret-token" not in health.text
        assert client.get("/api/experiments").status_code == 503
        models = client.get("/api/models")
        assert models.json()["error"]
        assert "secret-token" not in models.text


def test_get_does_not_queue_and_invalid_requests_do_not_queue():
    store = Store()
    settings = Settings(bedrock_model_id="test")
    with TestClient(create_app(settings=settings, store_factory=lambda _: store)) as client:
        assert client.get("/api/experiments").status_code == 200
        assert not store.jobs
        spec = specification().model_dump(mode="json")
        spec["row_limit"] = 51
        assert client.post("/api/experiments", json=spec).status_code == 422
        assert not store.created and not store.jobs
        spec["row_limit"] = 1
        response = client.post("/api/experiments", json=spec)
    assert response.status_code == 202
    experiment_id = response.json()["experiment_id"]
    assert store.created == [experiment_id]
    assert store.jobs == [("experiment", {"experiment_id": experiment_id})]


def test_experiments_and_batches_are_refused_when_live_execution_is_disabled():
    store = Store()
    app = create_app(
        settings=Settings(bedrock_model_id="test"),
        store_factory=lambda _: store,
        live_enabled=False,
    )
    with TestClient(app) as client:
        spec = specification().model_dump(mode="json")
        assert client.post("/api/experiments", json=spec).status_code == 403
        batch = {"dataset_id": "00000000-0000-0000-0000-000000000001"}
        assert client.post("/api/jobs/batch", json=batch).status_code == 403
    assert not store.jobs


def test_first_configuration_request_seeds_and_activates_the_yaml_version():
    store = Store()
    settings = Settings(bedrock_model_id="test")
    with TestClient(create_app(settings=settings, store_factory=lambda _: store)) as client:
        first = client.get("/api/config").json()
        second = client.get("/api/config").json()
    assert first["active_config_id"] == second["active_config_id"] == store.active
    assert len(store.configs) == 1


def test_recorded_import_preserves_actual_relevance_and_absent_history(tmp_path):
    import json

    from test_experiments import MemoryStore
    from test_trace import insight

    from mel.bundle import Bundle
    from mel.config import Settings
    from mel.explorer.api import import_recorded

    class RecordedStore(MemoryStore):
        def create_experiment(self, key, spec, source="live"):
            self.source = source
            super().create_experiment(key, spec, source)

    bundle = Bundle.load(Settings(bedrock_model_id="test"))
    path = tmp_path / "recorded.json"
    path.write_text(
        json.dumps(
            {
                "manifest": {"name": "Real record"},
                "bundle": bundle.snapshot,
                "results": [
                    {
                        "input": insight(),
                        "outcome": {
                            "insight_id": insight()["insight_id"],
                            "status": "rejected",
                            "review": {
                                "relevance_score": 0.4,
                                "language_correct": True,
                                "gives_new_advice": False,
                                "asks_personal_info": False,
                                "issues": [],
                            },
                        },
                    }
                ],
                "provenance": {"source": "recorded", "recorded_at": "2026-11-03"},
            }
        )
    )
    store = RecordedStore()
    import_recorded(path, store)
    outcome = next(iter(store.runs.values()))["results"][0]["outcome"]
    assert store.source == "recorded"
    assert outcome["review"]["relevance_score"] == 0.4
    assert outcome["events"] == []
    assert not outcome["usage_complete"]


def test_queued_jobs_are_dispatched_and_a_failed_dispatch_fails_the_job():
    store = Store()
    settings = Settings(bedrock_model_id="test")
    started = []

    def dispatcher(_, job_id):
        started.append(job_id)

    def broken(_, job_id):
        raise RuntimeError("no capacity")

    spec = specification().model_dump(mode="json")
    app = create_app(settings=settings, store_factory=lambda _: store, dispatcher=dispatcher)
    with TestClient(app) as client:
        job_id = client.post("/api/experiments", json=spec).json()["job_id"]
    assert started == [job_id]
    app = create_app(settings=settings, store_factory=lambda _: store, dispatcher=broken)
    with TestClient(app) as client:
        response = client.post("/api/experiments", json=spec)
    assert response.status_code == 502
    assert store.finished[1:] == ("failed", "Dispatch failed: RuntimeError")


def test_an_existing_draft_of_the_yaml_version_is_activated_not_duplicated():
    from mel.bundle import Bundle

    store = Store()
    settings = Settings(bedrock_model_id="test")
    draft = store.save_config(Bundle.load(settings).definition, note="Saved by mel-worker run")
    with TestClient(create_app(settings=settings, store_factory=lambda _: store)) as client:
        assert client.get("/api/config").json()["active_config_id"] == draft
    assert list(store.configs) == [draft]
