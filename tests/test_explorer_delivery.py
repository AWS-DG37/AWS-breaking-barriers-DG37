import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from mel.experiments import compare_runs
from mel.explorer.api import create_app


def test_packaged_recording_preserves_all_compared_candidates_and_failures():
    from test_experiments import MemoryStore

    from mel.explorer.api import import_recorded

    path = Path("src/mel/explorer/demo/recorded-run.json")
    store = MemoryStore()
    import_recorded(path, store)
    outcomes = [r["results"][0]["outcome"] for r in store.runs.values()]
    assert len(outcomes) == 3
    assert [o["status"] for o in outcomes] == ["accepted", "failed", "accepted"]
    assert outcomes[1]["reason"] == "UnexpectedModelBehavior"
    assert all(r["binding"]["purpose"] == "experiment" for r in store.runs.values())
    assert outcomes[0]["events"]
    assert outcomes[0]["question_evidence"]


def test_browser_resources_are_served_at_the_urls_in_the_page():
    with TestClient(create_app(live_enabled=False)) as client:
        page = client.get("/")
        assert page.status_code == 200
        for path in re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text):
            resource = client.get(path)
            assert resource.status_code == 200, path
            assert resource.headers["content-type"] != "text/html; charset=utf-8"


def test_golden_analysis_reads_actual_nested_rows_and_preserves_case_coverage():
    path = Path("eval/golden_holdout.jsonl")
    row = json.loads(path.read_text().splitlines()[0])
    detail = {
        "spec": {"evaluation_reference": "golden", "insights": [row["insight"]]},
        "runs": [
            {
                "run_id": "run",
                "label": "candidate",
                "model_id": "candidate",
                "results": [
                    {
                        "input": row["insight"],
                        "outcome": {
                            "status": "accepted" if row["expected"]["survey"] else "skipped",
                            "usage_complete": False,
                        },
                    }
                ],
            }
        ],
    }
    report = compare_runs(detail)
    assert report["cases"][0]["expected"] == row["expected"]
    assert report["cases"][0]["decision_agreement"] is True


def test_saved_outputs_can_be_reanalyzed_without_changing_the_execution_reference():
    row = json.loads(Path("eval/golden_holdout.jsonl").read_text().splitlines()[0])
    detail = {
        "spec": {"evaluation_reference": "rules", "insights": [row["insight"]]},
        "runs": [
            {
                "label": "A",
                "results": [
                    {
                        "input": row["insight"],
                        "outcome": {"status": "skipped", "usage_complete": False},
                    }
                ],
            }
        ],
    }
    report = compare_runs(detail, reference="golden")
    assert report["reference"] == "golden"
    assert report["reference_snapshot"]["sha256"]
    assert report["cases"][0]["expected"] == row["expected"]
    assert detail["spec"]["evaluation_reference"] == "rules"


def test_reference_checks_are_reused_as_diagnostic_evidence():
    fixture = json.loads(Path("src/mel/explorer/demo/recorded-run.json").read_text())
    runs = [
        {**r, "label": c["label"]} for r, c in zip(fixture["runs"], fixture["spec"]["candidates"])
    ]
    report = compare_runs({"spec": fixture["spec"], "runs": runs})
    checks = report["cases"][0]["deterministic_checks"]
    assert checks["assessor"] == "code"
    assert checks["checks"]["item_count_ok"] is True
    assert checks["reference"] == "legacy participant pack"
    assert report["cases"][1]["deterministic_checks"]["assessment"] == "not_assessed"
