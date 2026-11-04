import asyncio
import copy

import pytest
from pydantic import ValidationError
from test_trace import dependencies, insight

from mel.config import Settings
from mel.experiments import ExperimentSpec, compare_runs, execute_experiment, resolve_context


def specification():
    return ExperimentSpec.model_validate(
        {
            "name": "Offline comparison",
            "insights": [insight()],
            "candidates": [
                {"label": "A", "model": {"model_id": "test-a"}},
                {"label": "B", "model": {"model_id": "test-b"}},
            ],
            "reviewer": {"model_id": "reviewer"},
        }
    )


class MemoryStore:
    def __init__(self):
        self.spec = None
        self.runs = {}
        self.status = None

    def create_experiment(self, key, spec, source="live"):
        self.spec = spec

    def finish_experiment(self, key, status):
        self.status = status

    def start_run(self, key, fingerprint, manifest, **binding):
        self.runs[key] = {"manifest": manifest, "binding": binding, "results": []}

    def completed(self, key, retry_failed=False):
        return set()

    def save(self, key, row, outcome):
        self.runs[key]["results"].append({"input": row, "outcome": outcome.model_dump(mode="json")})

    def results(self, key):
        return [r["outcome"] for r in self.runs[key]["results"]]

    def finish(self, key, status):
        self.runs[key]["status"] = status


def test_context_preserves_insight_facts_and_does_not_infer_missing_crop():
    row = insight()
    row["asset"]["name"] = None
    original = copy.deepcopy(row)
    context = resolve_context(
        row,
        [{"farmer_id": row["farmer_id"], "crops": ["wheat"], "language": "en-IN"}],
        [{"crop": "wheat"}],
    )
    assert context["crop_calendar"] == []
    assert context["profile"]["crops"] == ["wheat"]
    assert context["conflicts"]
    assert row == original


def test_experiment_uses_same_graph_for_each_candidate_with_immutable_purpose():
    store = MemoryStore()

    def agents(*args):
        deps = dependencies()
        return deps.generator, deps.reviewer

    asyncio.run(
        execute_experiment(
            specification(),
            settings=Settings(bedrock_model_id="test"),
            store=store,
            agent_factory=agents,
        )
    )
    assert len(store.runs) == 2
    assert store.status == "completed"
    for run in store.runs.values():
        assert run["binding"]["purpose"] == "experiment"
        assert run["results"][0]["outcome"]["status"] == "accepted"
        assert run["results"][0]["outcome"]["events"]


def test_comparison_keeps_failures_and_unknown_usage_and_does_not_make_human_scores():
    detail = {
        "spec": {
            "evaluation_reference": "golden",
            "row_limit": 3,
            "insights": [{"insight_id": "i1"}, {"insight_id": "i2"}],
        },
        "runs": [
            {
                "label": "A",
                "model_id": "a",
                "results": [
                    {
                        "input": {"insight_id": "i1"},
                        "outcome": {
                            "status": "failed",
                            "usage_complete": False,
                            "input_tokens": 100,
                            "output_tokens": 20,
                        },
                    }
                ],
            }
        ],
        "assessments": [],
    }
    compared = compare_runs(detail)
    row = compared["rows"][0]
    assert row["total"] == 2
    assert row["failed"] == 1
    assert row["missing"] == 1
    assert row["input_tokens"] is None
    assert row["mean_relevance"] is None
    assert compared["reference"] == "golden"
    assert not compared["assessments"]


@pytest.mark.parametrize(
    "change",
    [
        {"row_limit": 0},
        {"row_limit": 51},
        {"candidates": []},
        {"name": " "},
        {"candidates": [{"label": "A", "model": {"model_id": " "}}]},
    ],
)
def test_bounds_rejected(change):
    value = specification().model_dump()
    value.update(change)
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(value)


def test_unavailable_candidate_keeps_all_cases_and_does_not_taint_next_run():
    store = MemoryStore()

    def agents(config, *args):
        if config.generation.model_id == "test-a":
            raise RuntimeError("private provider message")
        deps = dependencies()
        return deps.generator, deps.reviewer

    asyncio.run(
        execute_experiment(
            specification(),
            settings=Settings(bedrock_model_id="test"),
            store=store,
            agent_factory=agents,
        )
    )
    first, second = store.runs.values()
    assert first["status"] == "failed"
    assert first["results"][0]["outcome"]["reason"] == "RuntimeError"
    assert second["status"] == "completed"
    assert store.status == "completed_with_errors"


def test_evaluation_targets_cannot_enter_generation_payload():
    value = specification().model_dump()
    value["insights"][0]["expected"] = {"survey": True}
    with pytest.raises(ValidationError, match="evaluation targets"):
        ExperimentSpec.model_validate(value)


def test_comparison_counts_candidates_that_never_started():
    compared = compare_runs({"spec": specification().model_dump(mode="json"), "runs": []})
    assert len(compared["rows"]) == 2
    assert all(r["missing"] == r["total"] == 1 for r in compared["rows"])
    assert all(r["input_tokens"] is None for r in compared["rows"])
