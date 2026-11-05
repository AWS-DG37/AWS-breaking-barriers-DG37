"""The verifier's effort setting reaches Bedrock, and only for the verifier."""

import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mel.config import Settings
from mel.explorer import agent_runtime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
import service  # noqa: E402
import sweep  # noqa: E402

EXAMPLE = ROOT / "agents/skills/mel-survey-writer/references/examples/1-mustard-aphid-spray"
INSIGHT = json.loads((EXAMPLE / "insight.json").read_text())
SURVEY = (EXAMPLE / "survey.hi-IN.json").read_text()
PASS = json.dumps({"verdict": "pass", "relevance": 0.9, "checks": {}, "issues": []})


def test_bedrock_gets_output_config_effort_only_when_asked(monkeypatch):
    built = []

    class Model:
        def __init__(self, **kwargs):
            built.append(kwargs)

    class Agent:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, _user):
            return Result()

    class Result:
        metrics = None

        def __str__(self):
            return PASS

    monkeypatch.setitem(sys.modules, "strands", types.SimpleNamespace(Agent=Agent))
    monkeypatch.setitem(sys.modules, "strands.models", types.SimpleNamespace(BedrockModel=Model))
    sweep._call("verify", "global.anthropic.claude-sonnet-5-5", "s", "u", "low")
    sweep._call("verify", "global.anthropic.claude-sonnet-5-5", "s", "u")
    assert built[0]["additional_request_fields"] == {"output_config": {"effort": "low"}}
    assert "additional_request_fields" not in built[1]


def test_effort_goes_to_the_verifier_and_not_the_generator(monkeypatch):
    calls = []

    def fake_call(role, model_id, system, user, effort=None):
        calls.append((role, effort))
        return (SURVEY if role == "generate" else PASS), 1, 1, 0.0

    monkeypatch.setattr(sweep, "_call", fake_call)
    result = service.generate_survey(INSIGHT, verifier_effort="low")
    assert result["status"] == "accepted"
    assert calls == [("generate", None), ("verify", "low")]
    assert result["models"]["verifier_effort"] == "low"


def test_an_unknown_effort_is_refused_before_any_model_call(monkeypatch):
    monkeypatch.setattr(sweep, "_call", lambda *a, **k: pytest.fail("model was called"))
    with pytest.raises(service.BadRequest, match="verifier_effort"):
        service.generate_survey(INSIGHT, verifier_effort="max")


def test_console_forwards_the_verifier_effort_to_the_agent():
    payloads = []

    def invoke(payload):
        payloads.append(payload)
        return {"ok": True, "result": {"status": "accepted"}}

    app = FastAPI()
    agent_runtime.install(
        app, Settings(), invoker=invoke, dispatch=lambda run_id, value, run: run(run_id, value)
    )
    insight_id = next(iter(agent_runtime.golden_insights()))
    with TestClient(app) as http:
        body = {"insight_id": insight_id, "verifier_effort": "low"}
        assert http.post("/api/agents/runs", json=body).status_code == 202
        refused = http.post("/api/agents/runs", json={**body, "verifier_effort": "max"})
        assert refused.status_code == 422
    assert payloads[0]["verifier_effort"] == "low"
