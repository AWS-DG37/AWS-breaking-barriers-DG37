import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_experiments import MemoryStore

from mel.config import Settings
from mel.explorer.api import create_app


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is required")
@pytest.mark.parametrize(
    ("port", "override", "allowed_origins"),
    [
        (None, None, ["http://localhost:8080", "http://127.0.0.1:8080"]),
        ("8877", None, ["http://localhost:8877", "http://127.0.0.1:8877"]),
        (
            "8877",
            '["http://127.0.0.1:8877","http://127.0.0.1:8878"]',
            ["http://127.0.0.1:8877", "http://127.0.0.1:8878"],
        ),
    ],
)
def test_compose_recorded_import_accepts_only_configured_origins(
    port, override, allowed_origins, monkeypatch
):
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEL_")}
    if port is not None:
        env["MEL_EXPLORER_PORT"] = port
    if override is not None:
        env["MEL_EXPLORER_ALLOWED_ORIGINS"] = override
    rendered = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "config", "--format", "json"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    environment = json.loads(rendered.stdout)["services"]["explorer"]["environment"]
    for key in os.environ:
        if key.startswith("MEL_"):
            monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    settings = Settings()

    class RecordedStore(MemoryStore):
        def initialize(self):
            pass

        def close(self):
            pass

    store = RecordedStore()
    with TestClient(create_app(settings=settings, store_factory=lambda _: store)) as client:
        for origin in allowed_origins:
            response = client.post(
                "/api/import-recorded",
                headers={"origin": origin, "x-mel-intent": "operator"},
                json={},
            )
            assert response.status_code == 200, response.text
            assert store.runs
            assert all(
                run["binding"]["experiment_id"] == response.json()["experiment_id"]
                for run in store.runs.values()
            )
        rejected_origins = {
            "https://foreign.example",
            "http://127.0.0.1:8765",
            "http://localhost:8877",
            "http://127.0.0.1:8878",
        } - set(allowed_origins)
        for origin in rejected_origins:
            response = client.post(
                "/api/import-recorded",
                headers={"origin": origin, "x-mel-intent": "operator"},
                json={},
            )
            assert response.status_code == 403
    assert settings.explorer_allowed_origins == allowed_origins
