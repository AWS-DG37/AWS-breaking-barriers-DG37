"""The relevance judge: dev-only calibration, test gating, and counts. No model calls."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from mel.evaluation import relevance
from mel.evaluation.policy import load_split

ROOT = Path(__file__).resolve().parents[1]
SPLIT = ROOT / "eval/prompt_example_split.json"
RUBRIC = (ROOT / "eval/relevance_rubric.txt").read_text()
MODEL = "test.judge"


def _criterion(verdict):
    return relevance.Criterion(verdict=verdict, rationale="test")


async def oracle(message: str) -> relevance.RelevanceJudgment:
    """A deterministic judge that reads the controls the way the rubric intends."""
    body = json.loads(message)
    questions = [item["question"] for item in body["survey"]]
    reference = {item["question"] for item in body["reference"] or []}
    generic = questions == [relevance.GENERIC_ITEM["prompt"]]
    injected = {relevance.NEW_ADVICE_ITEM["prompt"], relevance.PERSONAL_ITEM["prompt"]}
    grounded = not generic and set(questions) - injected <= reference
    routes = [
        (o["label"], o["follow_up"]) for item in body["survey"]
        for o in item.get("options", []) if o.get("follow_up")
    ]
    wrong = {(label, code) for label, code in routes if label in relevance.POSITIVE_LABELS}
    wrong |= set(routes) & set(relevance.WRONG_CODE_ROUTES.items())
    # A question that routes every answer, the best one included, routes wrongly.
    wrong |= {"all" for item in body["survey"] if item.get("options")
              and all(o.get("follow_up") for o in item["options"])}
    routing = "not_applicable" if not routes else ("not_met" if wrong else "met")
    met = lambda ok: "met" if ok else "not_met"  # noqa: E731
    return relevance.RelevanceJudgment(
        advice_grounding=_criterion(met(grounded)),
        measures_adoption=_criterion(met(not generic)),
        measures_outcome=_criterion(met(not generic and relevance.has_outcome_question(
            [{"prompt": item["question"], "options": item.get("options", [])}
             for item in body["survey"]]
        ))),
        follow_up_routing=_criterion(routing),
        no_new_advice=_criterion(met(relevance.NEW_ADVICE_ITEM["prompt"] not in questions)),
        no_personal_info=_criterion(met(relevance.PERSONAL_ITEM["prompt"] not in questions)),
        reference_coverage="test",
    )


async def always_met(message: str) -> relevance.RelevanceJudgment:
    return relevance.RelevanceJudgment(
        **{c: _criterion("met") for c in relevance.CRITERIA}, reference_coverage="test",
    )


def calibrate(judge, **kwargs):
    return asyncio.run(relevance.calibrate(
        judge, split_path=SPLIT, rubric=RUBRIC, judge_model=MODEL, **kwargs,
    ))


def score(results, part, **kwargs):
    kwargs = {"judge": oracle, "rubric": RUBRIC, "judge_model": MODEL, **kwargs}
    return asyncio.run(relevance.score(
        kwargs.pop("judge"), results, split_path=SPLIT, part=part, **kwargs,
    ))


def _rows(part):
    manifest, source, _ = load_split(SPLIT)
    return [source[key] for key in manifest["dev_ids" if part == "dev" else "holdout_ids"]]


def _result(row, status="accepted", items=None):
    items = items if items is not None else relevance.reference_items(row["expected"])
    return {"insight_id": row["insight"]["insight_id"], "status": status,
            "survey": {"items": items} if status in relevance.GENERATED else None}


def test_controls_use_dev_rows_only_and_cover_every_criterion():
    manifest, _, _ = load_split(SPLIT)
    cases = relevance.controls(_rows("dev"))
    assert {c["golden_id"] for c in cases} <= set(manifest["dev_ids"])
    covered = {criterion for c in cases for criterion in c["verdicts"]}
    assert covered == set(relevance.CRITERIA)
    assert {c["control"] for c in cases} >= {
        "own_reference", "unrelated_reference", "generic_question", "positive_routing",
        "problem_routing", "wrong_code_routing", "injected_new_advice",
        "injected_personal_info", "scale_routing", "scale_wrong_routing",
    }


def test_scale_follow_ups_reach_the_judge_as_option_follow_ups():
    scale = {"prompt": "How is your wheat now?", "scale_min": 1, "scale_max": 5,
             "follow_up_at_or_below": 5, "follow_up_reason": "could_not_reach_expert"}
    (item,) = json.loads(relevance.render(_rows("dev")[0]["insight"], [scale], None))["survey"]
    assert [o["label"] for o in item["options"]] == ["1", "2", "3", "4", "5"]
    assert all(o["follow_up"] == "could_not_reach_expert" for o in item["options"])
    (item,) = json.loads(relevance.render(
        _rows("dev")[0]["insight"], [{**scale, "follow_up_at_or_below": 2}], None,
    ))["survey"]
    assert ["follow_up" in o for o in item["options"]] == [True, True, False, False, False]


def test_routing_controls_follow_the_generator_follow_up_contract():
    prompt = (ROOT / "prompts/generate.txt").read_text()
    assert "A Worse outcome answer carries crop_or_animal_damaged" in prompt
    cases = relevance.controls(_rows("dev"))
    for case in cases:
        codes = {(o["label"], o.get("follow_up")) for item in case["items"]
                 for o in item.get("options", []) if o.get("follow_up")}
        if case["control"] == "problem_routing":
            assert ("Worse", "advice_did_not_work") not in codes
        if case["control"] == "wrong_code_routing":
            assert codes == {("Worse", "advice_did_not_work")}


def test_the_judge_message_leaves_out_identifiers():
    row = _rows("dev")[0]
    message = relevance.render(row["insight"], [], row["expected"])
    for key in ("farmer_id", "conversation_id", "location", "insight_id"):
        assert key not in json.loads(message)["insight"]


def test_an_agreeing_judge_passes_calibration_and_a_lenient_one_fails():
    good = calibrate(oracle)
    assert good["passed"] is True
    assert good["part"] == "dev" and good["disagreements"] == []
    assert all(m["numerator"] == m["denominator"] > 0 for m in good["agreement"].values())
    lenient = calibrate(always_met, max_cases=5)
    assert lenient["passed"] is False
    assert lenient["dev_survey_rows"] == 5
    assert {d["criterion"] for d in lenient["disagreements"]} >= {
        "advice_grounding", "no_new_advice", "no_personal_info",
    }


@pytest.mark.parametrize("verdict", ["met", "not_met"])
def test_a_constant_outcome_judge_fails_calibration(verdict):
    async def constant(message):
        judgment = await oracle(message)
        return judgment.model_copy(update={"measures_outcome": _criterion(verdict)})

    report = calibrate(constant)
    assert report["passed"] is False
    outcome = report["agreement"]["measures_outcome"]
    assert outcome["numerator"] < outcome["denominator"]


def test_a_failed_call_fails_calibration_without_hiding_other_controls():
    calls = 0

    async def flaky(message):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("throttled")
        return await oracle(message)

    report = calibrate(flaky, max_cases=3, concurrency=1)
    assert report["failed_calls"] == 1 and report["passed"] is False


def test_the_test_half_needs_a_matching_passing_calibration():
    rows = _rows("test")
    results = [_result(row) for row in rows if row["expected"]["survey"]][:3]
    with pytest.raises(ValueError, match="calibration"):
        score(results, "test")
    passing = calibrate(oracle, max_cases=3)
    with pytest.raises(ValueError, match="rubric_sha256"):
        score(results, "test", calibration=passing, rubric=RUBRIC + "\nchanged")
    with pytest.raises(ValueError, match="judge_model"):
        score(results, "test", calibration=passing, judge_model="other.judge")
    with pytest.raises(ValueError, match="did not pass"):
        score(results, "test", calibration={**passing, "passed": False})
    report = score(results, "test", calibration=passing)
    assert report["calibrated"] is True
    assert report["core_pass"] == {"kind": "assessed", "numerator": 3, "denominator": 3}


def test_results_from_the_other_half_are_rejected():
    dev_row = next(row for row in _rows("dev") if row["expected"]["survey"])
    with pytest.raises(ValueError, match="outside the test half"):
        score([_result(dev_row)], "test", calibration=calibrate(oracle, max_cases=1))


def test_counts_sit_beside_every_rate():
    rows = _rows("dev")
    want = [row for row in rows if row["expected"]["survey"]]
    skip = [row for row in rows if not row["expected"]["survey"]]
    results = [
        _result(want[0]),
        _result(want[1], items=[relevance.GENERIC_ITEM]),
        _result(want[2], status="skipped"),
        _result(skip[0], items=[relevance.GENERIC_ITEM]),
    ]
    report = score(results, "dev")
    assert report["calibrated"] is False
    assert report["counts"] == {
        "results": 4, "judged": 2, "failed_calls": 0,
        "expected_survey_but_none": 1, "survey_but_none_expected": 1,
    }
    assert report["core_pass"] == {"kind": "assessed", "numerator": 1, "denominator": 2}
    assert report["criteria"]["follow_up_routing"]["kind"] == "not_assessed"
    for measurement in report["core_pass_by_resolution_type"].values():
        assert measurement["denominator"] >= 1


def test_the_committed_dev_calibration_matches_the_committed_rubric_and_split():
    """Editing the rubric or the split needs a new dev calibration before test scoring."""
    report = json.loads((ROOT / "eval/relevance_calibration_dev.json").read_text())
    relevance.require_calibration(
        report, split_path=SPLIT, rubric=RUBRIC, judge_model=report["judge_model"],
    )
    assert report["dev_survey_rows"] == 47 and report["failed_calls"] == 0


def test_failed_judge_calls_still_count_their_usage(tmp_path, monkeypatch):
    from mel import agents, worker

    class Exhausted:
        async def run(self, message, usage):
            usage.requests += 3  # Retries spent before output validation gave up.
            usage.input_tokens += 30
            usage.output_tokens += 12
            raise RuntimeError("exceeded maximum retries for output validation")

    monkeypatch.setattr(agents, "build_agent", lambda *args, **kwargs: Exhausted())
    output = tmp_path / "calibration.json"
    monkeypatch.setattr(sys, "argv", [
        "mel-worker", "eval", "calibrate-relevance", "--max-cases", "1",
        "--split", str(SPLIT), "--rubric", str(ROOT / "eval/relevance_rubric.txt"),
        "--config", str(ROOT / "config/agent.yaml"), "--output", str(output),
    ])
    worker.main()
    report = json.loads(output.read_text())
    calls = report["controls"]
    assert report["failed_calls"] == calls and report["passed"] is False
    assert report["usage"] == {
        "requests": 3 * calls, "input_tokens": 30 * calls, "output_tokens": 12 * calls,
    }


def test_relevance_cli_refuses_the_test_half_before_any_model_call(tmp_path, monkeypatch):
    from mel import agents, worker

    class NoCalls:
        async def run(self, message, usage=None):
            raise AssertionError("the judge called a model")

    monkeypatch.setattr(agents, "build_agent", lambda *args, **kwargs: NoCalls())
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(_result(_rows("test")[0])) + "\n")
    monkeypatch.setattr(sys, "argv", [
        "mel-worker", "eval", "relevance", "--results", str(results), "--part", "test",
        "--split", str(SPLIT), "--rubric", str(ROOT / "eval/relevance_rubric.txt"),
        "--config", str(ROOT / "config/agent.yaml"), "--output", str(tmp_path / "out.json"),
    ])
    with pytest.raises(SystemExit) as error:
        worker.main()
    assert error.value.code == 1
    assert not (tmp_path / "out.json").exists()
