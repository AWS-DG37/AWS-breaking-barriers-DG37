"""The survey or no survey gate: rules (decide.py) or the Strands classifier agent."""

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
import service  # noqa: E402
import sweep  # noqa: E402
from decide import decide  # noqa: E402

from mel.config import Settings  # noqa: E402
from mel.explorer import agent_runtime  # noqa: E402

GOLDEN = [
    json.loads(line)
    for line in Path("eval/golden_dev.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
SURVEY_ROW = next(g for g in GOLDEN if decide(g["insight"])["survey"])
INSIGHT = SURVEY_ROW["insight"]
SURVEY = {
    "items": [
        {
            "item_id": "q1",
            "type": "survey-scale",
            "prompt": "How useful was the advice? 1 means not useful, 5 means very useful.",
            "scale_min": 1,
            "scale_max": 5,
            "follow_up_at_or_below": 1,
            "follow_up_reason": "advice_did_not_work",
        }
    ],
    "rationale": "Checks the advice.",
}
PASS = {"verdict": "pass", "relevance": 0.9, "issues": []}


@pytest.fixture
def model(monkeypatch):
    """Stands in for Bedrock. Set model.classifier_says to the classifier's raw reply."""

    class Fake:
        classifier_says = '{"rule": "R8", "reason": "Real advice to check."}'
        calls = []

        def __call__(self, role, model_id, system, user, effort=None):
            self.calls.append((role, model_id, user))
            text = {
                "classify": self.classifier_says,
                "generate": json.dumps(SURVEY),
                "verify": json.dumps(PASS),
            }[role]
            return text, 10, 5, 0.01

    fake = Fake()
    monkeypatch.setattr(sweep, "_call", fake)
    monkeypatch.setattr(sweep, "check", lambda survey, insight: ([], None))
    return fake


def roles(model):
    return [call[0] for call in model.calls]


def test_the_rules_gate_makes_no_classifier_call(model):
    r = sweep.run_pipeline(INSIGHT, "gen", "ver")
    assert r["gate"] == "rules" and r["decision_rule"] == "R8" and r["status"] == "accepted"
    assert roles(model) == ["generate", "verify"]


def test_the_classifier_decides_and_its_reason_is_kept(model):
    r = sweep.run_pipeline(INSIGHT, "gen", "ver", gate="classifier", classifier="cls")
    assert (r["decision_rule"], r["decision_survey"], r["status"]) == ("R8", True, "accepted")
    assert r["decision_reason"] == "Real advice to check."
    assert roles(model) == ["classify", "generate", "verify"]
    assert model.calls[0][1] == "cls" and INSIGHT["advice_summary"] in model.calls[0][2]
    assert r["stages"]["classify"]["calls"] == 1


def test_a_classifier_no_survey_skips_the_writer_even_when_the_rules_say_survey(model):
    model.classifier_says = '```json\n{"rule": "R3", "reason": "Only general information."}\n```'
    r = sweep.run_pipeline(INSIGHT, "gen", "ver", gate="classifier")
    assert (r["decision_rule"], r["decision_survey"], r["status"]) == ("R3", False, "skipped")
    assert roles(model) == ["classify"]


@pytest.mark.parametrize(
    "reply", ["I think a survey is fine", '{"rule": "R2"}', '{"rule": "R6"}', '["R8"]']
)
def test_an_unusable_classifier_answer_fails_instead_of_guessing(model, reply):
    model.classifier_says = reply
    r = sweep.run_pipeline(INSIGHT, "gen", "ver", gate="classifier")
    assert r["status"] == "failed" and r["decision_survey"] is None and "classifier" in r["error"]
    assert roles(model) == ["classify"]


def test_classify_imports_on_its_own():
    import subprocess

    agents = Path(__file__).resolve().parents[1] / "agents"
    code = "import classify; print(classify.parse('{\"rule\": \"R4\"}')['reason_code'])"
    out = subprocess.run([sys.executable, "-c", code], cwd=agents, capture_output=True, text=True)
    assert out.stdout.strip() == "waiting_for_details", out.stderr


def test_a_failed_classification_counts_as_a_decision_miss():
    failed = {
        "insight_id": INSIGHT["insight_id"],
        "status": "failed",
        "decision_rule": None,
        "decision_survey": None,
        "attempts": 0,
        "relevance": None,
    }
    summary = sweep.score_run([failed], {INSIGHT["insight_id"]: SURVEY_ROW})
    assert summary["decision_cases"] == 1 and summary["decision_accuracy"] == 0


def test_old_run_files_without_a_classify_stage_still_score():
    old = {
        "insight_id": INSIGHT["insight_id"],
        "status": "accepted",
        "decision_rule": "R8",
        "decision_survey": True,
        "attempts": 1,
        "relevance": 0.8,
        "elapsed_seconds": 2,
        "stages": {
            s: {"calls": 1, "input_tokens": 1, "output_tokens": 1, "seconds": 1}
            for s in ("generate", "translate", "verify")
        },
    }
    summary = sweep.score_run([old], {INSIGHT["insight_id"]: SURVEY_ROW})
    assert summary["decision_accuracy"] == 1 and summary["tokens"]["classify"]["input"] == 0


def test_the_service_allowlists_the_gate_and_the_classifier_model(model):
    with pytest.raises(service.BadRequest, match="gate"):
        service.generate_survey(INSIGHT, gate="vibes")
    with pytest.raises(service.BadRequest, match="classifier"):
        service.generate_survey(INSIGHT, gate="classifier", classifier="us.someone.else")
    r = service.generate_survey(INSIGHT, gate="classifier")
    assert r["models"]["classifier"] == service.DEFAULT_CLASSIFIER
    assert service.generate_survey(INSIGHT)["models"]["classifier"] is None
    assert service.options()["gates"] == ["rules", "classifier"]


def test_decide_only_answers_from_the_chosen_gate(model):
    rules = service.decide_only(INSIGHT)
    assert (rules["rule"], rules["gate"], rules["classifier"]) == ("R8", "rules", None)
    assert model.calls == []
    model.classifier_says = '{"rule": "R5", "reason": "No check needed."}'
    got = service.decide_only(INSIGHT, "classifier")
    assert (got["rule"], got["survey"], got["reason"], got["gate"]) == (
        "R5",
        False,
        "No check needed.",
        "classifier",
    )
    model.classifier_says = "no idea"
    with pytest.raises(service.DecisionFailed):
        service.decide_only(INSIGHT, "classifier")


def test_the_console_decide_route_passes_the_gate():
    sent = []

    def invoke(payload):
        sent.append(payload)
        return {"ok": True, "result": {"survey": True, "rule": "R8", "gate": payload["gate"]}}

    app = FastAPI()
    agent_runtime.install(app, Settings(), invoker=invoke)
    with TestClient(app) as http:
        plain = http.post("/api/agents/decide", json={"insight_id": INSIGHT["insight_id"]})
        chosen = http.post(
            "/api/agents/decide",
            json={"insight_id": INSIGHT["insight_id"], "gate": "classifier", "classifier": "cls"},
        )
    assert plain.json()["decision"]["gate"] == "rules" and "classifier" not in sent[0]
    assert chosen.status_code == 200 and (sent[1]["gate"], sent[1]["classifier"]) == (
        "classifier",
        "cls",
    )


def test_the_console_passes_the_gate_choice_to_agentcore():
    sent = []

    def invoke(payload):
        sent.append(payload)
        return {"ok": True, "result": {"status": "skipped"}}

    app = FastAPI()
    agent_runtime.install(
        app,
        Settings(),
        invoker=invoke,
        dispatch=lambda run_id, value, execute: execute(run_id, value),
    )
    with TestClient(app) as http:
        body = {"insight_id": INSIGHT["insight_id"], "gate": "classifier", "classifier": "cls"}
        assert http.post("/api/agents/runs", json=body).status_code == 202
        assert http.post("/api/agents/runs", json={**body, "gate": "vibes"}).status_code == 422
    assert (sent[0]["gate"], sent[0]["classifier"]) == ("classifier", "cls")
