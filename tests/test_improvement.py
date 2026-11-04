import asyncio
import copy

import pytest
from improvement_fakes import edit, improver_factory
from pydantic import ValidationError
from test_experiments import MemoryStore
from test_trace import dependencies, insight

from mel.bundle import Bundle
from mel.config import AllowedModel, ModelConfig, Settings
from mel.improvement import (
    ConfigProposal,
    ImprovementSpec,
    apply_proposal,
    execute_improvement,
    review_target,
)

SETTINGS = Settings(bedrock_model_id="test")
DEFINITION = Bundle.load(SETTINGS).definition
FIRST_LINE = DEFINITION["prompts"]["generate"].splitlines()[0]


def proposal(**changes) -> ConfigProposal:
    return ConfigProposal.model_validate({**edit(FIRST_LINE, "Rewritten opening."), **changes})


def test_proposal_replaces_one_exact_passage_and_keeps_everything_else():
    changed = apply_proposal(DEFINITION, proposal(), SETTINGS)
    assert changed["prompts"]["generate"].startswith("Rewritten opening.")
    assert changed["prompts"]["review"] == DEFINITION["prompts"]["review"]
    assert changed["config"] == DEFINITION["config"]
    assert DEFINITION["prompts"]["generate"].startswith(FIRST_LINE)


@pytest.mark.parametrize(
    ("find", "message"),
    [("not in the prompt at all", "0 times"), ("the", "times")],
)
def test_proposal_find_text_must_occur_exactly_once(find, message):
    with pytest.raises(ValueError, match=message):
        apply_proposal(DEFINITION, proposal(**edit(find, "x")), SETTINGS)


def test_proposal_that_changes_nothing_is_refused():
    with pytest.raises(ValueError, match="changes nothing"):
        apply_proposal(DEFINITION, proposal(**edit(FIRST_LINE, FIRST_LINE)), SETTINGS)


def test_settings_are_validated_and_models_change_only_to_allowed_choices():
    effort = {"path": "generation.reasoning_effort", "value": "high", "reason": "r"}
    changed = apply_proposal(DEFINITION, proposal(setting_changes=[effort]), SETTINGS)
    assert changed["config"]["generation"]["reasoning_effort"] == "high"
    bad = {**effort, "value": "extreme"}
    with pytest.raises(ValidationError):
        apply_proposal(DEFINITION, proposal(setting_changes=[bad]), SETTINGS)
    model = {"path": "review.model_id", "value": "bigger", "reason": "r"}
    with pytest.raises(ValueError, match="models cannot change"):
        apply_proposal(DEFINITION, proposal(setting_changes=[model]), SETTINGS)
    allowed = Settings(
        bedrock_model_id="test",
        allowed_models=[
            AllowedModel(model_id="test", api="responses"),
            AllowedModel(model_id="bigger", api="responses"),
        ],
    )
    assert (
        apply_proposal(DEFINITION, proposal(setting_changes=[model]), allowed)["config"]["review"][
            "model_id"
        ]
        == "bigger"
    )
    with pytest.raises(ValueError, match="not enabled"):
        apply_proposal(DEFINITION, proposal(setting_changes=[{**model, "value": "other"}]), allowed)


def test_review_target_is_the_survey_or_rejected_draft_and_question_must_exist():
    survey = {"items": [{"item_id": "q1"}]}
    assert review_target({"survey": survey}, "q1") is survey
    assert review_target({"candidate_survey": survey}, None) is survey
    with pytest.raises(ValueError, match="no survey"):
        review_target({"status": "skipped"}, None)
    with pytest.raises(ValueError, match="does not belong"):
        review_target({"survey": survey}, "q9")


class ImprovementStore(MemoryStore):
    """Experiments, runs and improvement rounds in memory."""

    def __init__(self):
        super().__init__()
        self.rounds, self.finished = [], None

    def start_improvement(self, key):
        self.rounds = []

    def add_improvement_round(self, key, round_):
        self.rounds.append(round_)

    def finish_improvement(self, key, status, **result):
        self.finished = (status, result)


def comments(row: dict, count: int = 2) -> list[dict]:
    survey = {"items": [{"item_id": "q1", "prompt": "Did you spray?"}]}
    return [
        {
            "item_id": f"c{n}",
            "insight_id": row["insight_id"],
            "question_id": "q1" if n == 0 else None,
            "comment": f"comment {n}",
            "should_be": None,
            "insight": row,
            "survey": survey,
        }
        for n in range(count)
    ]


def spec(base: str, compare: str, rounds: int) -> ImprovementSpec:
    return ImprovementSpec(
        list_id="00000000-0000-0000-0000-000000000001",
        base_config_id=base,
        compare_config_id=compare,
        improver=ModelConfig(model_id="improver"),
        rounds=rounds,
        items=comments(insight()),
    )


def agents(*args):
    deps = dependencies()
    return deps.generator, deps.reviewer


def run(store, improvement, factory):
    return asyncio.run(
        execute_improvement(
            improvement,
            store=store,
            settings=SETTINGS,
            improvement_id="11111111-2222-3333-4444-555555555555",
            agent_factory=agents,
            improver_factory=factory,
        )
    )


def test_loop_keeps_only_better_rounds_and_compares_the_best_with_the_chosen_version():
    store = ImprovementStore()
    base = store.save_config(copy.deepcopy(DEFINITION))
    other = copy.deepcopy(DEFINITION)
    other["config"]["review"]["max_tokens"] = 999
    compare = store.save_config(other)
    seen = []
    factory = improver_factory(
        [
            edit("missing text", "x"),  # Inapplicable: sent back to the model once.
            edit(FIRST_LINE, "Round one opening."),
            edit("Round one opening.", "Round two opening."),
        ],
        # Base, comparison, round 1 (better: kept), round 2 (worse: dropped).
        [0.2, 0.4, 0.9, 0.5],
        seen,
    )
    verdict = run(store, spec(base, compare, rounds=2), factory)

    assert [r["round"] for r in store.rounds] == [0, 1, 2]
    assert [e["score"] for e in store.rounds[0]["evaluations"]] == [0.2, 0.4]
    first, second = store.rounds[1], store.rounds[2]
    assert (first["kept"], second["kept"]) == (True, False)
    assert second["parent_config_id"] == first["evaluations"][0]["config_id"]
    # Round 2 was proposed from the kept round-1 version, knowing its history.
    assert seen[-1]["previous_attempts"][0]["kept"] is True
    assert "Round one opening." in seen[-1]["current_version"]["prompts"]["generate"]
    assert seen[0]["review_items"][0]["comment"] == "comment 0"

    best = store.configs[first["evaluations"][0]["config_id"]]
    assert best["status"] == "draft" and best["parent_id"] == base
    assert best["definition"]["config"]["version"].endswith("-improve-11111111-r1")
    assert verdict["improved"] and verdict["score_delta"] == 0.5
    assert verdict["best"]["config_id"] == best["config_id"]
    assert store.finished[0] == "completed"
    assert store.finished[1]["best_config_id"] == best["config_id"]


def test_comments_without_an_accepted_survey_score_zero_without_a_judge_call():
    store = ImprovementStore()
    base = store.save_config(copy.deepcopy(DEFINITION))

    def rejecting(*args):
        poor = {
            "relevance_score": 0.1,
            "language_correct": True,
            "gives_new_advice": False,
            "asks_personal_info": False,
            "issues": ["unusable"],
        }
        deps = dependencies(poor)
        return deps.generator, deps.reviewer

    factory = improver_factory([edit(FIRST_LINE, "x")], [])  # No judge scores available.
    asyncio.run(
        execute_improvement(
            spec(base, base, rounds=1),
            store=store,
            settings=SETTINGS,
            improvement_id="11111111-2222-3333-4444-555555555555",
            agent_factory=rejecting,
            improver_factory=factory,
        )
    )
    (baseline,) = store.rounds[0]["evaluations"]
    assert baseline["score"] == 0 and baseline["counts"] == {"rejected": 1}
    assert {j["reason"] for j in baseline["judgements"]} == {"No accepted survey (rejected)."}
    assert store.rounds[1]["kept"] is False


def test_a_failed_loop_is_recorded_and_raised():
    store = ImprovementStore()
    base = store.save_config(copy.deepcopy(DEFINITION))
    factory = improver_factory([edit("missing", "x")] * 5, [0.5])
    with pytest.raises(Exception, match="output validation"):
        run(store, spec(base, base, rounds=1), factory)
    status, result = store.finished
    assert status == "failed" and "output validation" in result["error"]


def test_spec_limits_insights_and_needs_an_improver_model():
    row = insight()
    many = [
        {**c, "item_id": f"c{n}", "insight_id": f"i{n}", "insight": {**row, "insight_id": f"i{n}"}}
        for n, c in enumerate(comments(row, 1) * 51)
    ]
    with pytest.raises(ValidationError, match="at most 50"):
        ImprovementSpec.model_validate({**spec("a" * 32, "b" * 32, 1).model_dump(), "items": many})
    with pytest.raises(ValidationError, match="improver model"):
        ImprovementSpec.model_validate(
            {**spec("a" * 32, "b" * 32, 1).model_dump(), "improver": {"model_id": None}}
        )
