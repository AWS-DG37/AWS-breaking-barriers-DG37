"""Offline policy evidence must preserve provenance and difficult cases."""

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest

from mel import evaluation
from mel.bundle import Bundle
from mel.config import Settings
from mel.evaluation import policy as scorer
from mel.evaluation import split as partition

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bundle():
    return Bundle.load(Settings(config_path=ROOT / "config/agent.yaml"))


@pytest.fixture
def dataset(tmp_path):
    insight = json.loads(
        (ROOT / "docs/participant-pack/json/scenarios/1-mustard-aphid-spray/insight.json")
        .read_text()
    )
    rows = []
    for name in ("prompt", "eligible", "junk", "critical"):
        record = copy.deepcopy(insight)
        record["insight_id"] = f"I-{name}"
        # Distinct grouping keys, so the dev/holdout halves cannot leak.
        record.update(farmer_id=f"F-{name}", conversation_id=f"C-{name}")
        record["advice_summary"] += f" ({name})"
        if name == "junk":
            record.update(quality_flag="junk", created_at=None)
        if name == "critical":
            record["severity"] = "critical"
        expected = {"survey": name in {"prompt", "eligible"}, "rule": name}
        if expected["survey"]:
            expected["send_window"] = {
                "start": "2026-12-06T09:00:00+05:30",
                "end": "2026-12-09T19:00:00+05:30",
            }
        else:
            expected["reason_code"] = (
                "junk_insight" if name == "junk" else "critical_needs_human_call"
            )
        rows.append({"golden_id": f"G-{name}", "insight": record, "expected": expected})
    source = tmp_path / "docs/participant-pack/data/golden_set.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text("".join(json.dumps(r) + "\n" for r in rows))
    split = tmp_path / "eval/prompt_example_split.json"
    split.parent.mkdir()
    manifest = {
        "version": "test-split",
        "source": "docs/participant-pack/data/golden_set.jsonl",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "method": {"seed": 1},
        "prompt_example_ids": ["G-prompt"],
        "prompt_example_insight_ids": ["I-prompt"],
        "dev_ids": ["G-prompt"],
        "holdout_ids": ["G-eligible", "G-junk", "G-critical"],
    }
    split.write_text(json.dumps(with_counts(manifest, rows)))
    golden = split.parent / "golden_holdout.jsonl"
    golden.write_text("".join(json.dumps(r) + "\n" for r in rows[1:]))
    return SimpleNamespace(source=source, split=split, golden=golden, rows=rows)


def with_counts(manifest, rows):
    by_id = {row["golden_id"]: row for row in rows}
    manifest["counts"] = {
        half: partition.counts(by_id[key] for key in manifest[ids])
        for half, ids in (("dev", "dev_ids"), ("test", "holdout_ids"))
    }
    return manifest


def score(bundle, dataset):
    return evaluation.score_policy(
        bundle=bundle, golden_path=dataset.golden, split_path=dataset.split,
    )


def test_engine_covers_critical_cases_and_legacy_has_its_own_dataset(bundle, dataset):
    report = score(bundle, dataset)
    assert (report.eligibility.numerator, report.eligibility.denominator) == (3, 3)
    assert report.confusion == {
        "true_positive": 1, "true_negative": 2, "false_positive": 0, "false_negative": 0,
    }
    assert (report.legacy.eligibility.numerator, report.legacy.eligibility.denominator) == (3, 4)
    assert report.legacy.source_cases == 4
    assert report.per_rule["critical"].numerator == 1
    assert report.per_rule["critical"].denominator == 1
    assert (report.reason_agreement.numerator, report.reason_agreement.denominator) == (2, 2)
    assert {case.golden_id for case in report.cases} == {
        "G-eligible", "G-junk", "G-critical",
    }
    critical = next(c for c in report.cases if c.golden_id == "G-critical")
    assert critical.expected_reason == "critical_needs_human_call"
    assert critical.predicted_reason == "critical_needs_human_call"
    assert critical.reason_agreement.kind == "assessed"
    assert report.not_assessed["language_quality"].kind == "not_assessed"
    assert report.not_assessed["relevance_grounding"].kind == "not_assessed"


def test_real_production_rules_drive_predictions_without_changing_legacy(bundle, dataset):
    snapshot = copy.deepcopy(bundle.snapshot)
    snapshot["rules"]["window_rules"]["season_end"] = "2026-12-01"
    configured = Bundle(bundle.config, snapshot, bundle.contracts)
    report = score(configured, dataset)
    eligible = next(c for c in report.cases if c.golden_id == "G-eligible")
    assert eligible.predicted_survey is False
    assert eligible.predicted_reason == "window_after_season"
    assert report.confusion["false_negative"] == 1
    assert report.legacy.eligibility.numerator == 3


def test_window_comparison_uses_instants_and_reports_disagreement(bundle, dataset):
    report = score(bundle, dataset)
    assert (report.window_agreement.numerator, report.window_agreement.denominator) == (1, 1)
    row = dataset.rows[1]
    row["expected"]["send_window"]["start"] = "2026-12-06T11:00:00+05:30"
    dataset.source.write_text("".join(json.dumps(r) + "\n" for r in dataset.rows))
    manifest = json.loads(dataset.split.read_text())
    manifest["source_sha256"] = hashlib.sha256(dataset.source.read_bytes()).hexdigest()
    dataset.split.write_text(json.dumps(manifest))
    dataset.golden.write_text("".join(json.dumps(r) + "\n" for r in dataset.rows[1:]))
    report = score(bundle, dataset)
    assert (report.window_agreement.numerator, report.window_agreement.denominator) == (0, 1)


@pytest.mark.parametrize("start, succeeds", [
    ("2026-12-06T03:30:00+00:00", True),
    ("2026-12-06T09:00:00", False),
    ("2026-12-10T07:00:00+05:30", False),
])
def test_golden_window_requires_ordered_instants(bundle, dataset, start, succeeds):
    dataset.rows[1]["expected"]["send_window"]["start"] = start
    dataset.source.write_text("".join(json.dumps(r) + "\n" for r in dataset.rows))
    manifest = json.loads(dataset.split.read_text())
    manifest["source_sha256"] = hashlib.sha256(dataset.source.read_bytes()).hexdigest()
    dataset.split.write_text(json.dumps(manifest))
    dataset.golden.write_text("".join(json.dumps(r) + "\n" for r in dataset.rows[1:]))
    if succeeds:
        assert score(bundle, dataset).window_agreement.numerator == 1
    else:
        with pytest.raises(ValueError, match="window"):
            score(bundle, dataset)


def test_missing_legacy_assets_are_unassessed_without_hiding_primary_scores(
    bundle, dataset, monkeypatch,
):
    import dg_agent.checks as reference

    def missing():
        raise FileNotFoundError("reference pack not installed")

    monkeypatch.setattr(reference, "rules", missing)
    report = score(bundle, dataset)
    assert report.legacy.eligibility.kind == "not_assessed"
    assert "assets" in report.legacy.eligibility.reason
    assert report.eligibility.denominator == 3


def test_report_predictions_match_the_actual_worker_selection_boundary(bundle):
    from pydantic_graph import GraphRunContext

    from mel.graph import Generate, Select, State

    source = ROOT / "docs/participant-pack/data/golden_set.jsonl"
    insights = [json.loads(line)["insight"] for line in source.read_text().splitlines()]
    malformed = copy.deepcopy(insights[0])
    del malformed["farmer_id"]
    insights.append(malformed)
    engine = bundle.decision_engine()
    dependencies = SimpleNamespace(contracts=bundle.contracts, decisions=engine)

    async def compare():
        for insight in insights:
            state = State(insight, "S-00001", datetime.now(UTC))
            actual = await Select().run(GraphRunContext(state=state, deps=dependencies))
            predicted, reason, window = scorer._predict(engine, insight)
            assert predicted == isinstance(actual, Generate), insight["insight_id"]
            assert window == state.window
            if not predicted:
                assert reason == actual.data.reason

    asyncio.run(compare())


def test_source_changes_cannot_reuse_the_old_split(bundle, dataset):
    dataset.source.write_text(dataset.source.read_text() + "\n")
    with pytest.raises(ValueError, match="source"):
        score(bundle, dataset)


def test_holdout_labels_cannot_differ_from_the_hashed_source(bundle, dataset):
    rows = [json.loads(line) for line in dataset.golden.read_text().splitlines()]
    rows[0]["expected"]["survey"] = False
    dataset.golden.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(ValueError, match="source"):
        score(bundle, dataset)


@pytest.mark.parametrize("mutation", ["excluded", "missing", "duplicate"])
def test_holdout_must_match_the_declared_partition(bundle, dataset, mutation):
    rows = dataset.golden.read_text().splitlines()
    if mutation == "excluded":
        rows.append(json.dumps(dataset.rows[0]))
    elif mutation == "missing":
        rows.pop()
    else:
        rows.append(rows[0])
    dataset.golden.write_text("\n".join(rows) + "\n")
    with pytest.raises(ValueError, match="holdout|duplicate"):
        score(bundle, dataset)


def test_empty_holdout_is_unassessed_instead_of_perfect(bundle, dataset):
    manifest = json.loads(dataset.split.read_text())
    manifest["holdout_ids"] = []
    manifest["dev_ids"] = [row["golden_id"] for row in dataset.rows]
    dataset.split.write_text(json.dumps(with_counts(manifest, dataset.rows)))
    dataset.golden.write_text("")
    report = score(bundle, dataset)
    assert report.eligibility.kind == "not_assessed"
    assert report.reason_agreement.kind == "not_assessed"
    assert report.window_agreement.kind == "not_assessed"
    assert report.cases == ()


def test_unknown_golden_reason_counts_as_a_disagreement(bundle, dataset):
    dataset.rows[2]["expected"]["reason_code"] = "new_unreviewed_rule"
    dataset.source.write_text("".join(json.dumps(r) + "\n" for r in dataset.rows))
    manifest = json.loads(dataset.split.read_text())
    manifest["source_sha256"] = hashlib.sha256(dataset.source.read_bytes()).hexdigest()
    dataset.split.write_text(json.dumps(manifest))
    dataset.golden.write_text("".join(json.dumps(r) + "\n" for r in dataset.rows[1:]))
    report = score(bundle, dataset)
    assert (report.reason_agreement.numerator, report.reason_agreement.denominator) == (1, 2)


def test_scorer_never_constructs_an_aws_client(bundle, dataset, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline evaluation attempted AWS access")

    monkeypatch.setattr(boto3, "client", forbidden)
    monkeypatch.setattr(boto3, "Session", forbidden)
    assert score(bundle, dataset).eligibility.denominator == 3


def test_offline_cli_writes_report_without_starting_models_or_storage(
    dataset, tmp_path, monkeypatch,
):
    from mel import worker

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline CLI attempted a live service")

    monkeypatch.setattr(worker, "build_agents", forbidden)
    monkeypatch.setattr(worker, "Store", forbidden)
    monkeypatch.setattr(boto3, "Session", forbidden)
    output = tmp_path / "report.json"
    argv = [
        "mel-worker", "eval", "policy", "--golden", str(dataset.golden),
        "--split", str(dataset.split), "--output", str(output),
        "--config", str(ROOT / "config/agent.yaml"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    worker.main()
    report = json.loads(output.read_text())
    assert report["eligibility"]["denominator"] == 3
    before = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        worker.main()
    assert error.value.code != 0
    assert output.read_bytes() == before


def test_installed_cli_can_load_reference_from_outside_checkout(dataset, tmp_path):
    output = tmp_path / "installed-report.json"
    result = subprocess.run([
        sys.executable, "-I", "-c", "from mel.worker import main; main()", "eval", "policy",
        "--golden", str(dataset.golden), "--split", str(dataset.split),
        "--output", str(output), "--config", str(ROOT / "config/agent.yaml"),
    ], cwd=tmp_path, capture_output=True, text=True, timeout=20, env={
        **os.environ, "AWS_PROFILE": "offline-no-such-profile",
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["legacy"]["eligibility"]["kind"] == "assessed"
    assert report["legacy"]["eligibility"]["denominator"] == 4


def test_evaluation_edits_do_not_invalidate_generation_resume(bundle, tmp_path, monkeypatch):
    import mel.bundle as bundle_module

    package = tmp_path / "mel"
    package.mkdir()
    runtime = package / "worker.py"
    runtime.write_text("generation-v1")
    evaluator = package / "evaluation" / "__init__.py"
    evaluator.parent.mkdir()
    evaluator.write_text("evaluation-v1")
    monkeypatch.setattr(bundle_module, "__file__", str(package / "bundle.py"))
    before = bundle.fingerprint(b"input", "us-east-1")
    evaluator.write_text("evaluation-v2")
    assert bundle.fingerprint(b"input", "us-east-1") == before
    runtime.write_text("generation-v2")
    assert bundle.fingerprint(b"input", "us-east-1") != before


@pytest.mark.parametrize("mutation", ["overlap", "missing_dev", "wrong_prompt"])
def test_manifest_cannot_reintroduce_a_prompt_example(bundle, dataset, mutation):
    manifest = json.loads(dataset.split.read_text())
    if mutation == "overlap":
        manifest["holdout_ids"].append("G-prompt")
    elif mutation == "missing_dev":
        manifest["dev_ids"] = []
    else:
        manifest["prompt_example_insight_ids"] = ["I-wrong"]
    dataset.split.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="holdout|prompt"):
        score(bundle, dataset)


def test_manifest_cannot_read_a_source_outside_its_project(bundle, dataset):
    manifest = json.loads(dataset.split.read_text())
    manifest["source"] = "../outside.jsonl"
    dataset.split.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="source"):
        score(bundle, dataset)


def test_a_shared_farmer_across_halves_is_rejected(bundle, dataset):
    dataset.rows[0]["insight"]["farmer_id"] = "F-eligible"
    dataset.source.write_text("".join(json.dumps(r) + "\n" for r in dataset.rows))
    manifest = json.loads(dataset.split.read_text())
    manifest["source_sha256"] = hashlib.sha256(dataset.source.read_bytes()).hexdigest()
    dataset.split.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="farmer:F-eligible"):
        score(bundle, dataset)


def test_shared_advice_with_another_crop_name_is_rejected(bundle, dataset):
    rows = dataset.rows
    rows[1]["insight"]["asset"]["name"] = "wheat"
    rows[0]["insight"]["advice_summary"] = "Advised to spray the Wheat  field."
    rows[1]["insight"]["advice_summary"] = "advised to spray the mustard field."
    dataset.source.write_text("".join(json.dumps(r) + "\n" for r in rows))
    manifest = json.loads(dataset.split.read_text())
    manifest["source_sha256"] = hashlib.sha256(dataset.source.read_bytes()).hexdigest()
    dataset.split.write_text(json.dumps(manifest))
    dataset.golden.write_text("".join(json.dumps(r) + "\n" for r in rows[1:]))
    with pytest.raises(ValueError, match="advice:"):
        score(bundle, dataset)


def test_recorded_counts_must_match_the_rows(bundle, dataset):
    manifest = json.loads(dataset.split.read_text())
    manifest["counts"]["test"]["survey"] += 1
    dataset.split.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="counts"):
        score(bundle, dataset)


def test_report_labels_the_rules_score_as_a_regression_baseline(bundle, dataset):
    report = score(bundle, dataset)
    assert report.label.startswith("regression_baseline")
    assert (report.dev_cases, report.test_cases) == (1, 3)


def test_measurement_cannot_claim_more_successes_than_cases():
    with pytest.raises(ValueError, match="numerator"):
        scorer.Assessed(numerator=3, denominator=2)
