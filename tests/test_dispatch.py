"""Job dispatch: one Fargate task per job when a cluster is configured."""

import json
from pathlib import Path

import boto3
import pytest

from mel.bundle import Bundle
from mel.config import Settings
from mel.datasets import parse_insights
from mel.jobs import dispatch, estimate_batch


class Ecs:
    def __init__(self, failures=()):
        self.calls = []
        self.failures = list(failures)

    def run_task(self, **request):
        self.calls.append(request)
        return {"tasks": [{}], "failures": self.failures}


def test_without_a_cluster_dispatch_leaves_the_job_for_a_polling_runner(monkeypatch):
    monkeypatch.setattr(boto3, "client", lambda *a, **k: pytest.fail("no AWS call expected"))
    dispatch(Settings(), "job-1")


def test_dispatch_runs_the_worker_task_for_exactly_that_job(monkeypatch):
    ecs = Ecs()
    monkeypatch.setattr(boto3, "client", lambda service, **_: ecs)
    settings = Settings(
        job_cluster="cluster",
        job_task_definition="task:3",
        job_subnets=["subnet-1"],
        job_security_groups=["sg-1"],
    )
    dispatch(settings, "job-1")
    (request,) = ecs.calls
    assert request["taskDefinition"] == "task:3"
    assert request["overrides"]["containerOverrides"] == [
        {"name": "worker", "command": ["jobs", "--job-id", "job-1"]}
    ]
    network = request["networkConfiguration"]["awsvpcConfiguration"]
    assert network == {
        "subnets": ["subnet-1"],
        "securityGroups": ["sg-1"],
        "assignPublicIp": "ENABLED",
    }


def test_a_run_task_failure_is_raised(monkeypatch):
    monkeypatch.setattr(boto3, "client", lambda service, **_: Ecs([{"reason": "RESOURCE:CPU"}]))
    with pytest.raises(RuntimeError, match="RESOURCE:CPU"):
        dispatch(Settings(job_cluster="c", job_task_definition="t"), "job-1")


def test_job_subnets_are_read_from_json_environment_variables(monkeypatch):
    monkeypatch.setenv("MEL_JOB_SUBNETS", '["subnet-a"]')
    monkeypatch.setenv("MEL_JOB_SECURITY_GROUPS", '["sg-a"]')
    settings = Settings()
    assert (settings.job_subnets, settings.job_security_groups) == (["subnet-a"], ["sg-a"])


def test_the_estimate_counts_rule_decisions_and_model_calls_without_any_model():
    bundle = Bundle.load(Settings(bedrock_model_id="test"))
    insights = parse_insights(Path("tests/fixtures/smoke_insights.jsonl").read_bytes())
    plan = estimate_batch(bundle, insights)
    assert plan["selected"] == 10
    # The fixture's critical insight is now surveyed (R2 removed), so 7 surveys, not 6.
    assert plan["surveys"] == 7
    assert plan["skipped"] == {"R3": 1, "R5": 1, "R7": 1}
    revisions = bundle.config.graph.max_revisions
    assert plan["model_calls"] == {
        "min": 14,
        "max": 14 * (1 + revisions) * bundle.config.graph.max_requests_per_call,
    }
    assert plan["cost_usd"] is None


def test_the_estimate_prices_tokens_when_the_configuration_has_prices():
    bundle = Bundle.load(Settings(bedrock_model_id="test"))
    bundle.config.batch.input_cost_per_million = 1.0
    bundle.config.batch.output_cost_per_million = 10.0
    scenario = Path("docs/participant-pack/json/scenarios/1-mustard-aphid-spray/insight.json")
    insight = json.loads(scenario.read_text())
    plan = estimate_batch(bundle, [insight])
    assert plan["surveys"] == 1
    assert plan["cost_usd"] == round((6600 * 1.0 + 400 * 10.0) / 1_000_000, 2)
